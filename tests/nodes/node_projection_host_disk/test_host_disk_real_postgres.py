# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host disk projection's write path against real Postgres (OMN-23865).

Each case builds a disposable schema carrying the real migration, retargets the writer's two real statements at it and
applies readings through the real asyncpg adapter. A stand-in database cannot catch a column-type mismatch (a datetime
bound as text, a JSON column bound as a list); only a real connection enforces the types. Skipped, as the PR landing
projection's proof is, when INTEGRATION_POSTGRES_PASSWORD is not set.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.events.host_disk_reading import ModelHostDiskRead
from omnimarket.nodes.node_projection_host_disk.handlers import (
    handler_host_disk_writer as writer_module,
)
from omnimarket.nodes.node_projection_host_disk.handlers.handler_host_disk_writer import (
    HostDiskProjectionWriter,
)

from .host_disk_events import T0, TOPIC, later, reading

_MIGRATION = (
    Path(writer_module.__file__).resolve().parents[1]
    / "migrations"
    / "0000_create_host_disk.sql"
)
_SCHEMA_PREFIX = "omn23865_host_disk"
_STATEMENTS = {
    name: getattr(writer_module, name) for name in ("_UPSERT_CLASS", "_UPSERT_VOLUME")
}
_WRITE_TIME_COLUMNS = frozenset({"first_seen_at", "updated_at", "projection_cursor"})


def _dsn_or_skip() -> str:
    secret = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not secret:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping the host disk write-path proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"


class _Schema:
    """One disposable schema carrying the real migration."""

    def __init__(self, dsn: str, name: str) -> None:
        self.dsn, self.name = dsn, name

    def scoped(self, statement: str) -> str:
        return statement.replace("omninode_internal.", f"{self.name}.")

    async def _run(self, *statements: str) -> None:
        conn = await asyncpg.connect(self.dsn)
        try:
            for statement in statements:
                await conn.execute(statement)
        finally:
            await conn.close()

    def reset(self) -> None:
        asyncio.run(
            self._run(
                f"DROP SCHEMA IF EXISTS {self.name} CASCADE",
                f"CREATE SCHEMA {self.name}",
                self.scoped(_MIGRATION.read_text(encoding="utf-8")),
            )
        )

    def drop(self) -> None:
        asyncio.run(self._run(f"DROP SCHEMA IF EXISTS {self.name} CASCADE"))

    def fetch(self, query: str) -> list[dict[str, Any]]:
        async def go() -> list[dict[str, Any]]:
            conn = await asyncpg.connect(self.dsn)
            try:
                rows = await conn.fetch(self.scoped(query))
            finally:
                await conn.close()
            return [
                {k: v for k, v in dict(r).items() if k not in _WRITE_TIME_COLUMNS}
                for r in rows
            ]

        return asyncio.run(go())

    def apply(self, monkeypatch: pytest.MonkeyPatch, event: Any) -> dict[str, Any]:
        from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

        for name, original in _STATEMENTS.items():
            monkeypatch.setattr(writer_module, name, self.scoped(original))
        writer = HostDiskProjectionWriter()
        data = json.loads(event.model_dump_json())

        async def go() -> dict[str, Any]:
            db = AsyncpgAdapter(dsn=self.dsn, min_size=1, max_size=2)
            await db.connect()
            try:
                return await writer._project_event(TOPIC, data, db)
            finally:
                await db.close()

        return asyncio.run(go())


@pytest.fixture
def schema() -> Iterator[_Schema]:
    made = _Schema(_dsn_or_skip(), f"{_SCHEMA_PREFIX}_{os.getpid()}")
    made.reset()
    yield made
    made.drop()


def _cache(schema: _Schema) -> dict[str, Any]:
    (row,) = schema.fetch(
        "SELECT * FROM omninode_internal.host_disk_class WHERE host_name = 'h202' AND class_name = 'uv_cache'"
    )
    return row


@pytest.mark.integration
def test_a_reading_lands_with_typed_columns(
    schema: _Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = schema.apply(monkeypatch, reading())
    assert outcome["rows_upserted"] == 8
    row = _cache(schema)
    assert row["read_at"] == T0
    assert (row["entry_count"], row["revision_count"], row["reading_count"]) == (
        670,
        1,
        1,
    )
    assert row["groups"] is not None


@pytest.mark.integration
def test_an_older_reading_never_replaces_a_newer_row(
    schema: _Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema.apply(monkeypatch, reading(at=later(5), cache_copies=100))
    outcome = schema.apply(monkeypatch, reading(at=T0, cache_copies=670))
    assert outcome["rows_upserted"] == 0
    assert _cache(schema)["entry_count"] == 100


@pytest.mark.integration
def test_a_redelivery_writes_nothing_and_a_newer_reading_counts(
    schema: _Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema.apply(monkeypatch, reading())
    assert schema.apply(monkeypatch, reading())["rows_upserted"] == 0
    schema.apply(monkeypatch, reading(at=later(5), cache_copies=100))
    row = _cache(schema)
    assert (row["entry_count"], row["reading_count"], row["read_at"]) == (
        100,
        2,
        later(5),
    )


@pytest.mark.integration
def test_replaying_the_same_readings_twice_yields_identical_rows(
    schema: _Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    readings = [reading(), reading(at=later(5), cache_copies=100)]
    for event in readings:
        schema.apply(monkeypatch, event)
    first = schema.fetch(
        "SELECT * FROM omninode_internal.host_disk_class ORDER BY host_name, class_name"
    )
    for event in readings:
        schema.apply(monkeypatch, event)
    assert (
        schema.fetch(
            "SELECT * FROM omninode_internal.host_disk_class ORDER BY host_name, class_name"
        )
        == first
    )


@pytest.mark.integration
def test_the_writer_reads_the_shared_reading_model() -> None:
    assert writer_module.ModelHostDiskRead is ModelHostDiskRead
