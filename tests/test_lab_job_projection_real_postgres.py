# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20604: the lab job writer against real Postgres.

The stale-write guard is the conflict arm's WHERE and the append-only log is an
ON CONFLICT DO NOTHING: there is no Python branch for either, so a recording
double cannot prove them. These tests drive the real writer entry, one event
loop per message, with the real migration applied to a throwaway schema.

Real Postgres, never SQLite: SQLite accepts a string into a timestamp column,
which is the hole a write-path proof exists to close. The harness SKIPS without
a reachable database; CI provisions one for integration-marked tests and fails
a missing-service skip (OMN-14172).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.nodes.node_projection_lab_job.handlers import LabJobProjectionWriter
from omnimarket.nodes.node_projection_lab_job.handlers import (
    handler_lab_job_writer as writer_module,
)
from tests.test_lab_job_projection import S, life, transitioned

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_lab_job"
    / "migrations"
    / "0000_create_lab_job.sql"
)
_SCHEMA_PREFIX = "omn20604_lab_job"

#: The writer's real statements, captured before any test retargets them.
_STATEMENTS: dict[str, str] = {
    name: getattr(writer_module, name)
    for name in ("_UPSERT_STATE", "_APPEND_TRANSITION")
}

#: Columns the database assigns at write time, excluded from a replay compare.
_WRITE_TIME_COLUMNS = frozenset(
    {"first_seen_at", "updated_at", "recorded_at", "projection_cursor"}
)


def _dsn_or_skip() -> str:
    secret = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not secret:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping the lab job "
            "write-path proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Schema:
    """One disposable schema carrying the real migration."""

    def __init__(self, dsn: str, name: str) -> None:
        self.dsn = dsn
        self.name = name

    def scoped(self, statement: str) -> str:
        return statement.replace("omninode_internal.", f"{self.name}.")

    async def _reset(self) -> None:
        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {self.name} CASCADE")
            await conn.execute(f"CREATE SCHEMA {self.name}")
            await conn.execute(self.scoped(_MIGRATION.read_text(encoding="utf-8")))
        finally:
            await conn.close()

    async def _drop(self) -> None:
        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {self.name} CASCADE")
        finally:
            await conn.close()

    async def _fetch(self, query: str) -> list[dict[str, Any]]:
        conn = await asyncpg.connect(self.dsn)
        try:
            rows = await conn.fetch(self.scoped(query))
        finally:
            await conn.close()
        return [
            {k: v for k, v in dict(row).items() if k not in _WRITE_TIME_COLUMNS}
            for row in rows
        ]

    def state(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = _run(
            self._fetch("SELECT * FROM omninode_internal.lab_job_state ORDER BY job_id")
        )
        return rows

    def transitions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = _run(
            self._fetch(
                "SELECT * FROM omninode_internal.lab_job_transitions "
                "ORDER BY job_id, seq"
            )
        )
        return rows

    def writer(self, monkeypatch: pytest.MonkeyPatch) -> LabJobProjectionWriter:
        """The real writer, its two statements retargeted at this schema."""
        for name, original in _STATEMENTS.items():
            monkeypatch.setattr(writer_module, name, self.scoped(original))
        instance = LabJobProjectionWriter()
        instance.bind_projection_database_url(self.dsn)
        return instance


@pytest.fixture
def schema_factory() -> Iterator[Any]:
    dsn = _dsn_or_skip()
    made: list[_Schema] = []

    def _make(suffix: str = "a") -> _Schema:
        schema = _Schema(dsn, f"{_SCHEMA_PREFIX}_{os.getpid()}_{suffix}")
        _run(schema._reset())
        made.append(schema)
        return schema

    yield _make
    for schema in made:
        _run(schema._drop())


def _apply(writer: LabJobProjectionWriter, events: list[dict[str, Any]]) -> list[int]:
    return [writer.handle(dict(event))["rows_upserted"] for event in events]


@pytest.mark.integration
def test_replaying_the_same_events_twice_yields_identical_rows(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = life()
    once = schema_factory("once")
    writer = once.writer(monkeypatch)
    assert _apply(writer, events) == [2] * len(events)
    state, log = once.state(), once.transitions()
    assert _apply(writer, events) == [0] * len(events)
    assert once.state() == state
    assert once.transitions() == log

    fresh = schema_factory("fresh")
    _apply(fresh.writer(monkeypatch), events)
    assert fresh.state() == state
    assert fresh.transitions() == log


@pytest.mark.integration
def test_an_older_or_equal_seq_cannot_replace_the_state(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = schema_factory()
    writer = schema.writer(monkeypatch)
    writer.handle(transitioned(5, S.DONE))
    state = schema.state()
    assert writer.handle(transitioned(4, S.RUNNING))["rows_upserted"] == 1
    assert writer.handle(transitioned(5, S.FAILED))["rows_upserted"] == 0
    assert schema.state() == state
    assert [row["seq"] for row in schema.transitions()] == [4, 5]


@pytest.mark.integration
def test_migration_is_idempotent_and_checks_every_state(
    schema_factory: Any,
) -> None:
    schema = schema_factory()

    async def check() -> None:
        conn = await asyncpg.connect(schema.dsn)
        try:
            await conn.execute(schema.scoped(_MIGRATION.read_text()))
            for state in S:
                await conn.execute(
                    schema.scoped(
                        "INSERT INTO omninode_internal.lab_job_state "
                        "(job_id, kind, state, episode, attempt, seq, entered_state_at) "
                        "VALUES ($1, 'adopted', $2, 1, 1, 1, NOW())"
                    ),
                    state.value,
                    state.value,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    schema.scoped(
                        "UPDATE omninode_internal.lab_job_state SET state = 'invalid'"
                    )
                )
        finally:
            await conn.close()

    _run(check())
