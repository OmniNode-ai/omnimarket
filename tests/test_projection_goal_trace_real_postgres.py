# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20034: the goal-trace writer's SQL, proven against real Postgres.

The in-memory writer tests answer the writer's statements with a Python
stand-in, so the SQL is never run there. Three properties live only in SQL: the
composite key and its ``ON CONFLICT`` clause (a redelivery inserts nothing, the
same id with another digest inserts a second row), the scope read with its
``ANY($2::uuid[])`` predicate, and the bound types -- UUID, TIMESTAMPTZ,
TEXT[], JSONB -- which only a real driver over the extended protocol enforces.

The harness mirrors ``test_omn19513_claude_hook_events_real_postgres.py``: it
SKIPS without a reachable database and applies the node's real migration to a
disposable schema, retargeting ``omninode_internal``.

The AC3 falsifier is the last test: the count of rows for the replayed goal is
zero before the replay and positive after, and a second replay leaves it equal.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_goal_trace.handlers.handler_goal_trace_writer import (
    GoalTraceProjectionWriter,
)

_ROOT = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_MIGRATIONS = (
    _ROOT
    / "node_projection_claude_hook_events"
    / "migrations"
    / "0000_create_claude_hook_events.sql",
    _ROOT
    / "node_projection_goal_trace"
    / "migrations"
    / "0000_create_goal_trace_events.sql",
)
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_hook_capture"
_SCHEMA = "omn20034_goal_trace_write_path_test"
GOAL = UUID("11111111-1111-4111-8111-111111111111")
T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping goal-trace write-path DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    credentials = f"{quote_plus(user)}:{quote_plus(password)}"
    if host.startswith("/"):  # a unix-socket directory
        dsn = f"postgresql://{credentials}@/{db}?host={quote_plus(host)}&port={port}"
    else:
        dsn = f"postgresql://{credentials}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for goal-trace proof: {exc}")


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


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    for migration in _MIGRATIONS:
        await conn.execute(_scoped(migration.read_text(encoding="utf-8")))


def _hook(agent: str, minute: int, *, goal: UUID | None) -> dict[str, Any]:
    name = "PreToolUse.goal_binding.json" if goal else "PreToolUse.json"
    event: dict[str, Any] = json.loads((_FIXTURES / "events" / name).read_text("utf-8"))
    event["event_id"] = str(uuid4())
    event["emitted_at"] = (T0 + timedelta(minutes=minute)).isoformat()
    event["lineage"]["session_id"] = "s1"
    event["lineage"]["agent_id"] = agent
    event["lineage"]["is_subagent"] = True
    event["lineage"]["tool_use_id"] = f"t{minute}"
    if goal:
        event["payload"]["goal_id"] = str(goal)
        event["payload"].pop("parent_goal_id")
    return event


def _replay_messages() -> list[dict[str, Any]]:
    claim = {
        "event_id": str(GOAL),
        "kind": "claim",
        "body_digest": "d-claim",
        "emitted_at": T0.isoformat(),
        "goal_id": str(GOAL),
    }
    return [
        _hook("lane", 3, goal=None),
        _hook("lane", 4, goal=None),
        _hook("lane", 5, goal=GOAL),
        _hook("stray", 6, goal=None),
        claim,
    ]


async def _count(conn: asyncpg.Connection) -> int:
    value = await conn.fetchval(
        f"SELECT count(*) FROM {_SCHEMA}.goal_trace_events WHERE goal_id = $1", GOAL
    )
    return int(value)


@pytest.mark.integration
async def test_a_replay_writes_this_replays_rows_for_the_goal() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        writer = GoalTraceProjectionWriter()
        writer._db = _ScopedConnectionAdapter(conn)  # type: ignore[assignment]
        before = await _count(conn)
        messages = _replay_messages()
        for message in messages:
            await writer._project(writer.db, "topic", message)
        after = await _count(conn)
        assert before == 0
        assert after == 4  # the claim, the binding hook and the two hooks it claims
        checksum = await conn.fetchval(
            f"SELECT checksum FROM {_SCHEMA}.goal_traces WHERE goal_id = $1", GOAL
        )
        assert str(checksum).startswith("sha256:")
        stray = await conn.fetchval(
            f"SELECT count(*) FROM {_SCHEMA}.goal_trace_events "
            "WHERE agent_key = 's1:stray' AND goal_id IS NULL"
        )
        assert stray == 1

        # A second replay of the same messages adds no event row and moves no checksum.
        for message in messages:
            await writer._project(writer.db, "topic", message)
        assert await _count(conn) == after
        assert (
            await conn.fetchval(
                f"SELECT checksum FROM {_SCHEMA}.goal_traces WHERE goal_id = $1", GOAL
            )
            == checksum
        )
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
