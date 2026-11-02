# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that the agent-actions node writes once per key on a rerun.

The golden chain in test_golden_chain_projection_agent_actions.py runs the same
stream on SQLite. This runs it through the node's runner on Postgres, where the
``::jsonb`` casts, the UUID and TIMESTAMPTZ column types and ``ON CONFLICT`` are
Postgres's own. It builds the node's schema in a throwaway schema and drops it.
It reads the database from the INTEGRATION_POSTGRES_* variables through the
``integration_postgres_dsn`` fixture and skips without them.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_agent_actions.handlers.handler_agent_actions_writer import (
    AgentActionsRowWriter,
)
from omnimarket.projection.runner import MessageMeta

_NODE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_agent_actions"
)
_STREAM = json.loads(
    (
        Path(__file__).parent / "fixtures" / "agent_actions" / "replayed_events.json"
    ).read_text(encoding="utf-8")
)
_EXPECTED = {
    "agent_actions": 3,
    "agent_transformation_events": 1,
    "router_performance_metrics": 1,
    "agent_detection_failures": 1,
    "agent_execution_logs": 1,
    "agent_status_events": 2,
}


async def _replay(runner: AgentActionsRowWriter) -> None:
    for event in _STREAM:
        meta = MessageMeta(
            partition=event["partition"],
            offset=event["offset"],
            fallback_id="",
            topic=event["topic"],
        )
        assert await runner.project_event(event["topic"], event["payload"], meta)


@pytest.mark.integration
async def test_two_replays_leave_one_row_per_key_on_postgres(
    integration_postgres_dsn: str,
) -> None:
    schema = f"omn21920_agent_actions_{uuid4().hex[:8]}"
    admin = await asyncpg.connect(integration_postgres_dsn)
    runner = AgentActionsRowWriter()
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}")
        # The migration schema-qualifies every relation to public; the throwaway
        # schema stands in for it so the lab database's public schema is not touched.
        await admin.execute(
            (_NODE / "migrations" / "0000_create_agent_observability_tables.sql")
            .read_text(encoding="utf-8")
            .replace("public.", f"{schema}.")
        )
        separator = "&" if "?" in integration_postgres_dsn else "?"
        runner.bind_projection_database_url(
            f"{integration_postgres_dsn}{separator}search_path={schema}"
        )
        await runner.db.connect()

        await _replay(runner)
        after_first = {
            table: await admin.fetch(f"SELECT * FROM {table} ORDER BY 1")
            for table in _EXPECTED
        }
        await _replay(runner)
        after_second = {
            table: await admin.fetch(f"SELECT * FROM {table} ORDER BY 1")
            for table in _EXPECTED
        }

        assert {t: len(rows) for t, rows in after_first.items()} == _EXPECTED
        # execution logs set updated_at = NOW() on a conflict; every other column
        # of every table is what the first replay wrote.
        for table, rows in after_second.items():
            assert len(rows) == _EXPECTED[table], table
            for before, now in zip(after_first[table], rows, strict=True):
                old, new = dict(before), dict(now)
                changed = {k for k in old if old[k] != new[k]}
                assert changed <= (
                    {"updated_at"} if table == "agent_execution_logs" else set()
                ), table
        log = after_second["agent_execution_logs"][0]
        assert log["status"] == "completed"
        assert log["project_path"] == "/work/omnimarket"
    finally:
        await runner.db.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()
