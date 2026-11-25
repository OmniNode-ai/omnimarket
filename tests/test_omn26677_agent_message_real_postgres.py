# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that the agent message projection writes once per partition and offset.

The unit tests run the INSERT on SQLite. This runs the node's runner on Postgres, where the TIMESTAMPTZ and BOOLEAN
column types, the CHECK constraints and ``ON CONFLICT ... RETURNING`` are Postgres's own. It builds the node's schema in a
throwaway schema and drops it. It reads the database from the INTEGRATION_POSTGRES_* variables through the
``integration_postgres_dsn`` fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_agent_message.handlers.handler_agent_message_writer import (
    AgentMessageRowWriter,
)
from omnimarket.projection.runner import MessageMeta

_NODE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_agent_message"
)
TOPIC = "onex.evt.omnimarket.agent-message.v1"


def _wire(text: str) -> dict[str, Any]:
    return {
        "from": "claude-h201",
        "to": "codex-h053",
        "thread": "proof",
        "kind": "message",
        "text": text,
        "sent_at": "2026-11-24T22:17:00+00:00",
        "platform": "claude",
    }


@pytest.mark.integration
async def test_replays_partitions_and_refusals_on_postgres(
    integration_postgres_dsn: str,
) -> None:
    schema = f"omn26677_agent_message_{uuid4().hex[:8]}"
    admin = await asyncpg.connect(integration_postgres_dsn)
    runner = AgentMessageRowWriter()
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}")
        await admin.execute(
            (_NODE / "migrations" / "0000_create_agent_messages.sql")
            .read_text(encoding="utf-8")
            .replace("public.", f"{schema}.")
        )
        separator = "&" if "?" in integration_postgres_dsn else "?"
        runner.bind_projection_database_url(
            f"{integration_postgres_dsn}{separator}search_path={schema}"
        )
        await runner.db.connect()

        deliveries: list[tuple[int, int, dict[str, Any]]] = [
            (0, 5, _wire("first")),
            (0, 5, _wire("replayed")),
            (1, 5, _wire("other partition")),
            (0, 6, {}),
            (0, 7, _wire("bad platform") | {"platform": "nope"}),
        ]
        for partition, offset, payload in deliveries:
            meta = MessageMeta(
                partition=partition, offset=offset, fallback_id="", topic=TOPIC
            )
            assert await runner.project_event(TOPIC, payload, meta)

        rows = await admin.fetch(
            "SELECT source_partition, source_offset, status, refusal_reason, text, sender_verified "
            "FROM agent_messages ORDER BY 1, 2"
        )
        assert [
            (
                r["source_partition"],
                r["source_offset"],
                r["status"],
                r["refusal_reason"],
            )
            for r in rows
        ] == [
            (0, 5, "ok", None),
            (0, 6, "refused", "dropped"),
            (0, 7, "refused", "refused"),
            (1, 5, "ok", None),
        ]
        by_key = {(r["source_partition"], r["source_offset"]): r for r in rows}
        assert by_key[(0, 5)]["text"] == "first"
        assert by_key[(1, 5)]["text"] == "other partition"
        assert not any(r["sender_verified"] for r in rows)
    finally:
        await runner.db.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()
