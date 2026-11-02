# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof of node_projection_context_audit's writer SQL.

The table is created exactly as omnibase_infra migration 053 creates it (a
BIGSERIAL id and no natural key), then the node's own migration extends it. Only
the schema name is isolated; the shipped SQL, asyncpg parameter binding and the
node migration are what run. The shared fixture uses CI's INTEGRATION_POSTGRES
service when configured, otherwise an owned, disposable PostgreSQL container.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

import tests.test_omn19514_ticket_id_projection_real_postgres as _pg
from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_context_audit.handlers import (
    handler_context_audit_writer as writer_module,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_golden_chain_projection_context_audit import _event

postgres = _pg.postgres

NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_context_audit"
)

# Verbatim from omnibase_infra docker/migrations/forward/053_create_context_audit_events.sql.
_INFRA_053_TABLE = """
CREATE TABLE IF NOT EXISTS public.context_audit_events (
    id                      BIGSERIAL       PRIMARY KEY,
    task_id                 TEXT            NOT NULL,
    parent_task_id          TEXT,
    correlation_id          TEXT            NOT NULL,
    contract_id             TEXT,
    event_type              TEXT            NOT NULL,
    enforcement_level       TEXT            NOT NULL,
    enforcement_action      TEXT,
    violation_details       JSONB,
    context_tokens_used     INT,
    context_budget_tokens   INT,
    return_tokens           INT,
    return_max_tokens       INT,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
"""

_RETIRED_COLUMNS = (
    "task_id",
    "parent_task_id",
    "correlation_id",
    "contract_id",
    "event_type",
    "enforcement_level",
    "enforcement_action",
    "violation_details",
    "context_tokens_used",
    "context_budget_tokens",
    "return_tokens",
    "return_max_tokens",
)


def _events() -> list[tuple[str, dict[str, Any]]]:
    topics = writer_module.ContextAuditProjectionWriter().subscribe_topics
    return [(topic, _event(index)) for index, topic in enumerate(topics)]


@pytest.mark.integration
async def test_replay_against_real_postgres_leaves_one_row_per_key(
    postgres: _pg._Postgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await asyncpg.connect(**postgres.connect_kwargs())
    schema = f"context_audit_{uuid4().hex}"
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(_INFRA_053_TABLE.replace("public.", f"{schema}."))
        migrations = sorted((NODE / "migrations").glob("*.sql"))
        assert [path.name for path in migrations] == [
            "0000_add_context_audit_event_key.sql"
        ]
        for migration in migrations:
            await admin.execute(migration.read_text().replace("public.", f"{schema}."))
        # The migration is safe to re-run.
        for migration in migrations:
            await admin.execute(migration.read_text().replace("public.", f"{schema}."))
        monkeypatch.setattr(
            writer_module,
            "_UPSERT",
            writer_module._UPSERT.replace("public.", f"{schema}."),
        )

        pool = await asyncpg.create_pool(
            **postgres.connect_kwargs(), min_size=1, max_size=2
        )
        assert pool is not None
        adapter = AsyncpgAdapter()
        monkeypatch.setattr(adapter, "_pool", pool)
        writer = writer_module.ContextAuditProjectionWriter()
        monkeypatch.setattr(writer, "_db", adapter)

        async def replay() -> None:
            for index, (topic, event) in enumerate(_events()):
                meta = MessageMeta(
                    partition=index % 2, offset=10 + index, fallback_id="f", topic=topic
                )
                assert await writer.project_event(topic, event, meta)

        await replay()
        rows = await admin.fetch(
            f"SELECT * FROM {schema}.context_audit_events ORDER BY task_id"
        )
        assert len(rows) == 5
        assert len({row["event_key"] for row in rows}) == 5
        for row, (_, event) in zip(
            rows, sorted(_events(), key=lambda e: e[1]["task_id"]), strict=True
        ):
            for column in _RETIRED_COLUMNS:
                got = row[column]
                if column == "violation_details" and got is not None:
                    got = json.loads(got)
                assert got == event[column], column
        first = {row["event_key"]: (row["id"], row["created_at"]) for row in rows}

        await replay()
        again = await admin.fetch(f"SELECT * FROM {schema}.context_audit_events")
        assert len(again) == 5
        assert {
            row["event_key"]: (row["id"], row["created_at"]) for row in again
        } == first

        # Rows the retired module wrote carry a NULL key and stay distinct.
        for _ in range(2):
            await admin.execute(
                f"INSERT INTO {schema}.context_audit_events "
                "(task_id, correlation_id, event_type, enforcement_level) "
                "VALUES ('legacy', 'c', 't', 'l')"
            )
        assert (
            await admin.fetchval(f"SELECT COUNT(*) FROM {schema}.context_audit_events")
            == 7
        )
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()
