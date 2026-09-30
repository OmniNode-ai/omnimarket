# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19833: the PR landing writer against real Postgres.

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

from omnimarket.events.pr_landing.model_pr_landing_transitioned import (
    ModelPrLandingTransitioned as SharedTransitioned,
)
from omnimarket.events.topics import PR_LANDING_TRANSITIONED_TOPIC_V1
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_transitioned import (
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_projection_pr_landing.handlers import (
    handler_pr_landing_writer as writer_module,
)
from omnimarket.nodes.node_projection_pr_landing.handlers.handler_pr_landing_writer import (
    PrLandingProjectionWriter,
)
from tests.pr_landing_projection_events import (
    HEAD_A,
    HEAD_B,
    PR,
    REPO,
    S,
    _wire,
    a_reopened_pr_life,
    agent_needed,
    at,
    closed,
    merged,
    transitioned,
)

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_pr_landing"
    / "migrations"
    / "0000_create_pr_landing.sql"
)
_SCHEMA_PREFIX = "omn19833_pr_landing"

#: The writer's real statements, captured before any test retargets them.
_STATEMENTS: dict[str, str] = {
    name: getattr(writer_module, name)
    for name in ("_UPSERT_STATE", "_APPEND_TRANSITION")
}

#: Columns the database assigns at write time, excluded from a replay compare.
_WRITE_TIME_COLUMNS = frozenset({"first_seen_at", "updated_at", "projection_cursor"})


def _dsn_or_skip() -> str:
    secret = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not secret:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping the PR landing "
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
            self._fetch(
                "SELECT * FROM omninode_internal.pr_landing_state "
                "ORDER BY repository, pr_number"
            )
        )
        return rows

    def transitions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = _run(
            self._fetch(
                "SELECT * FROM omninode_internal.pr_landing_transitions "
                "ORDER BY repository, pr_number, seq"
            )
        )
        return rows

    def writer(self, monkeypatch: pytest.MonkeyPatch) -> PrLandingProjectionWriter:
        """The real writer, its two statements retargeted at this schema."""
        for name, original in _STATEMENTS.items():
            monkeypatch.setattr(writer_module, name, self.scoped(original))
        instance = PrLandingProjectionWriter()
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


def _apply(
    writer: PrLandingProjectionWriter, events: list[dict[str, Any]]
) -> list[int]:
    return [writer.handle(dict(event))["rows_upserted"] for event in events]


@pytest.mark.integration
def test_a_transition_built_from_the_orchestrators_export_lands_in_postgres(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ModelPrLandingTransitioned is SharedTransitioned
    schema = schema_factory()
    payload = ModelPrLandingTransitioned(
        repository=REPO,
        pr_number=PR,
        head_sha=HEAD_A,
        seq=1,
        from_state=None,
        to_state=S.OBSERVED,
        trigger="pushed",
        transitioned_at=at(1),
    )
    event = _wire(payload, PR_LANDING_TRANSITIONED_TOPIC_V1, payload.seq * 10)

    assert _apply(schema.writer(monkeypatch), [event]) == [2]
    (row,) = schema.state()
    (transition,) = schema.transitions()
    expected = (
        payload.repository,
        payload.pr_number,
        payload.seq,
        payload.to_state.value,
    )
    assert (row["repository"], row["pr_number"], row["seq"], row["state"]) == expected
    assert (
        transition["repository"],
        transition["pr_number"],
        transition["seq"],
        transition["to_state"],
    ) == expected


# --------------------------------------------------------------------------
# AC1: a redelivered or older transition never replaces a newer row.
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_an_older_transition_never_replaces_a_newer_row(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = schema_factory()
    writer = schema.writer(monkeypatch)
    newer = transitioned(5, S.CHECKS_PENDING, S.READY, "verdict_green")
    older = transitioned(4, S.OBSERVED, S.CHECKS_PENDING, "evaluated_checks_required")

    first = writer.handle(dict(newer))
    second = writer.handle(dict(older))

    assert first["rows_upserted"] == 2
    assert second["state_write_refused"] is True
    assert second["rows_upserted"] == 1, "the older transition is logged, not applied"
    (row,) = schema.state()
    assert (row["seq"], row["state"], row["last_trigger"]) == (
        5,
        "READY",
        "verdict_green",
    )
    assert [r["seq"] for r in schema.transitions()] == [4, 5]


@pytest.mark.integration
def test_a_redelivered_transition_writes_nothing(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = schema_factory()
    writer = schema.writer(monkeypatch)
    event = transitioned(3, S.OBSERVED, S.CHECKS_PENDING, "evaluated_checks_required")

    assert writer.handle(dict(event))["rows_upserted"] == 2
    redelivered = writer.handle(dict(event))

    assert redelivered["rows_upserted"] == 0
    assert redelivered["state_write_refused"] is True
    assert len(schema.transitions()) == 1


@pytest.mark.integration
def test_an_older_episode_closed_after_a_newer_merged_is_refused(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10: the outbox can emit an older episode's closed after a newer merged."""
    schema = schema_factory()
    writer = schema.writer(monkeypatch)
    _apply(writer, a_reopened_pr_life())

    late = writer.handle(closed(3, 0))

    assert late["state_write_refused"] is True
    (row,) = schema.state()
    assert (row["state"], row["episode"], row["seq"]) == ("MERGED", 1, 11)


# --------------------------------------------------------------------------
# Episodes and the two halves of one transition.
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_a_reopened_pr_carries_two_terminals(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = schema_factory()
    writer = schema.writer(monkeypatch)
    _apply(writer, a_reopened_pr_life())

    (row,) = schema.state()
    assert row["state"] == "MERGED"
    assert row["episode"] == 1
    assert row["head_sha"] == HEAD_B
    assert row["last_trigger"] == "merged"
    assert row["terminal_at"] == at(11)
    assert row["agent_reason"] is None, "cleared when the row left NEEDS_AGENT"

    log = schema.transitions()
    assert [r["seq"] for r in log] == list(range(1, 12))
    terminals = [
        (r["seq"], r["to_state"]) for r in log if r["to_state"] in ("MERGED", "CLOSED")
    ]
    assert terminals == [(3, "CLOSED"), (11, "MERGED")]
    assert [r["seq"] for r in log if r["opens_episode"]] == [4]


@pytest.mark.integration
@pytest.mark.parametrize("transition_first", [True, False])
def test_the_two_halves_of_one_transition_merge_in_either_order(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch, transition_first: bool
) -> None:
    schema = schema_factory("first" if transition_first else "second")
    writer = schema.writer(monkeypatch)
    _apply(
        writer,
        [transitioned(5, S.OBSERVED, S.CHECKS_PENDING, "evaluated_checks_required")],
    )
    halves = [
        transitioned(6, S.CHECKS_PENDING, S.NEEDS_AGENT, "verdict_real_red"),
        agent_needed(6, detail="lint"),
    ]
    _apply(writer, halves if transition_first else list(reversed(halves)))

    (row,) = schema.state()
    assert row["seq"] == 6
    assert row["state"] == "NEEDS_AGENT"
    assert row["last_trigger"] == "verdict_real_red"
    assert row["agent_reason"] == "real_red"
    assert row["agent_detail"] == "lint"
    assert row["head_sha"] == HEAD_A


@pytest.mark.integration
def test_a_merged_after_a_reopen_counts_the_episode_once(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLOSED -> MERGED opens the episode, and the terminal re-asserts it."""
    orders = {
        "transition_first": [
            transitioned(8, S.CLOSED, S.MERGED, "merged"),
            merged(8, 1),
        ],
        "terminal_first": [
            merged(8, 1),
            transitioned(8, S.CLOSED, S.MERGED, "merged"),
        ],
    }
    for name, tail in orders.items():
        schema = schema_factory(name)
        writer = schema.writer(monkeypatch)
        _apply(
            writer,
            [transitioned(7, S.CHECKS_PENDING, S.CLOSED, "closed"), closed(7, 0)],
        )
        _apply(writer, tail)
        (row,) = schema.state()
        assert (row["state"], row["episode"], row["seq"]) == ("MERGED", 1, 8), name


# --------------------------------------------------------------------------
# AC3: replaying the same events twice yields identical rows.
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_replaying_the_same_events_twice_yields_identical_rows(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    life = a_reopened_pr_life()

    once = schema_factory("once")
    writer = once.writer(monkeypatch)
    _apply(writer, life)
    state_once, log_once = once.state(), once.transitions()

    # The same events again, into the same tables: nothing moves.
    replay_counts = _apply(writer, life)
    assert once.state() == state_once
    assert once.transitions() == log_once
    assert sum(replay_counts) == 1, (
        "only the merged terminal at the final seq re-asserts its own values; no "
        "transition and no older event is re-applied"
    )

    # The same events into fresh tables: byte-identical rows.
    fresh = schema_factory("fresh")
    _apply(fresh.writer(monkeypatch), life)
    assert fresh.state() == state_once
    assert fresh.transitions() == log_once
    assert log_once[8]["intents"] is not None


@pytest.mark.integration
def test_the_row_reads_back_with_typed_column_values(
    schema_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = schema_factory()
    _apply(schema.writer(monkeypatch), a_reopened_pr_life())
    (row,) = schema.state()
    assert row["event_at"] == at(11)
    assert isinstance(row["seq"], int)
    log = schema.transitions()
    assert log[0]["from_state"] is None
    assert log[0]["transitioned_at"] == at(1)
