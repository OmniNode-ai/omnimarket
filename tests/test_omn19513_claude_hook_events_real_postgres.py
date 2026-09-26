# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19513: the claude-hook-events writer's SQL, proven against real Postgres.

Why this is not redundant with the in-memory scenario tests: those answer the
writer's statements with a Python stand-in, so the SQL itself is never run
there. Four properties of this projection live ONLY in SQL:

* the span merge (``LEAST``/``GREATEST``, a known parent replacing ``unknown``
  and never the reverse) is an ``ON CONFLICT DO UPDATE`` clause;
* ``tool_call_count`` is a recount subquery, which is what makes a replay
  converge rather than double-count;
* the event insert's replay guard is ``ON CONFLICT (event_id) DO NOTHING``;
* the bound types -- UUID, TIMESTAMPTZ, JSONB, TEXT[] -- are only enforced by a
  real driver over the extended query protocol.

The harness mirrors ``test_omn18768_runner_fleet_real_postgres_write_path.py``:
it SKIPS without a reachable database and applies the node's real migration to
a disposable schema, retargeting ``omninode_internal`` in the DDL and in the
writer's own statements.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.nodes.node_projection_claude_hook_events.handlers.handler_claude_hook_events_writer import (
    ClaudeHookEventsProjectionWriter,
)

_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_claude_hook_events"
)
_MIGRATION = _NODE_DIR / "migrations" / "0000_create_claude_hook_events.sql"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_hook_capture"
_SCHEMA = "omn19513_claude_hook_events_write_path_test"
_SCENARIOS = ("subagent_tree", "workflow_agent", "orphan_agent")


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping claude-hook-events "
            "write-path DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for claude-hook-events proof: {exc}")


def _scoped(statement: str) -> str:
    return statement.replace("omninode_internal.", f"{_SCHEMA}.").replace(
        "'omninode_internal'", f"'{_SCHEMA}'"
    )


class _ScopedConnectionAdapter:
    """The writer's adapter seam over one real connection, retargeted."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(_scoped(query), *params)
        return [dict(row) for row in rows]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    await conn.execute(_scoped(_MIGRATION.read_text(encoding="utf-8")))


async def _project_all(writer: ClaudeHookEventsProjectionWriter) -> None:
    for name in _SCENARIOS:
        for event in _jsonl(_FIXTURES / "scenarios" / f"{name}.events.jsonl"):
            await writer._project(
                "onex.evt.omniclaude.hook-event.v1", event
            )  # onex-topic-allow: the capture contract's metadata topic


def _iso(value: Any) -> Any:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


async def _event_rows(conn: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    rows = await conn.fetch(f"SELECT * FROM {_SCHEMA}.claude_hook_events")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = dict(row)
        record.pop("ingested_at")
        record.pop("projection_cursor")
        record["event_id"] = str(record["event_id"])
        record["correlation_id"] = str(record["correlation_id"])
        record["causation_id"] = (
            None if record["causation_id"] is None else str(record["causation_id"])
        )
        record["emitted_at"] = _iso(record["emitted_at"])
        record["payload"] = json.loads(record["payload"])
        record["content_ref_ids"] = list(record["content_ref_ids"])
        out[record["event_id"]] = record
    return out


async def _span_rows(conn: asyncpg.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    rows = await conn.fetch(f"SELECT * FROM {_SCHEMA}.claude_agent_spans")
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        record = dict(row)
        for column in ("first_seen_at", "updated_at", "projection_cursor"):
            record.pop(column)
        record["started_at"] = _iso(record["started_at"])
        record["stopped_at"] = _iso(record["stopped_at"])
        out[(record["session_id"], record["agent_id"])] = record
    return out


def _expected() -> tuple[
    dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]
]:
    events = {}
    for row in _jsonl(_FIXTURES / "expected_projection" / "claude_hook_events.jsonl"):
        row.pop("ingested_at")
        events[row["event_id"]] = row
    spans = {
        (row["session_id"], row["agent_id"]): row
        for row in _jsonl(
            _FIXTURES / "expected_projection" / "claude_agent_spans.jsonl"
        )
    }
    return events, spans


@pytest.mark.integration
async def test_scenarios_read_back_from_real_postgres() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        writer = ClaudeHookEventsProjectionWriter()
        writer._db = _ScopedConnectionAdapter(conn)  # type: ignore[assignment]

        await _project_all(writer)
        expected_events, expected_spans = _expected()
        assert await _event_rows(conn) == expected_events
        assert await _span_rows(conn) == expected_spans

        # A full replay converges: no new event rows, no double-counted calls.
        await _project_all(writer)
        assert await _event_rows(conn) == expected_events
        assert await _span_rows(conn) == expected_spans
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_the_subagent_flag_constraint_is_enforced_by_the_table() -> None:
    """The CHECK is the table's own guard, beneath the wire model's."""
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                f"INSERT INTO {_SCHEMA}.claude_hook_events (event_id, session_id, "
                "agent_id, is_subagent, hook_event_name, correlation_id, "
                "emitted_at, source_topic) VALUES (gen_random_uuid(), 's', NULL, "
                "true, 'Stop', gen_random_uuid(), NOW(), 't')"
            )
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
