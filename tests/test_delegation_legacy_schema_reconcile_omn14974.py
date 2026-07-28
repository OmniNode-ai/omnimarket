# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14974: warm-schema compatibility for delegation projections."""

from __future__ import annotations

import json
import os
from pathlib import Path

import asyncpg
import pytest

_MIGRATIONS = (
    Path(__file__).resolve().parent.parent
    / "src/omnimarket/nodes/node_projection_delegation/migrations"
)
_BASE = _MIGRATIONS / "0007_delegation_events.sql"
_METRICS = _MIGRATIONS / "0009_delegate_skill_projection_metrics.sql"
_RECONCILE = _MIGRATIONS / "0009a_delegation_events_legacy_schema_reconcile.sql"
_VIEWS = _MIGRATIONS / "0010_create_delegation_dashboard_projection_views.sql"

_LEGACY_SCHEMA = """
CREATE TABLE delegation_events (
    id UUID PRIMARY KEY,
    correlation_id TEXT NOT NULL UNIQUE,
    session_id TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    task_type TEXT NOT NULL DEFAULT '',
    delegated_to TEXT NOT NULL DEFAULT '',
    delegated_by TEXT,
    quality_gate_passed BOOLEAN NOT NULL DEFAULT FALSE,
    quality_gates_checked JSONB,
    quality_gates_failed JSONB,
    cost_usd NUMERIC NOT NULL DEFAULT 0,
    cost_savings_usd NUMERIC NOT NULL DEFAULT 0,
    delegation_latency_ms INTEGER,
    repo TEXT,
    is_shadow BOOLEAN NOT NULL DEFAULT FALSE,
    projected_at TIMESTAMPTZ,
    tenant_id TEXT
);
INSERT INTO delegation_events (
    id, correlation_id, quality_gates_checked, quality_gates_failed
) VALUES (
    gen_random_uuid(), 'legacy-correlation',
    '["contract", "quality"]'::jsonb, '["latency"]'::jsonb
), (
    gen_random_uuid(), 'unsupported-correlation',
    '{"contract": true}'::jsonb, '"latency"'::jsonb
);
"""


def test_reconcile_runs_before_dashboard_views() -> None:
    assert _RECONCILE.is_file()
    assert _METRICS.name < _RECONCILE.name < _VIEWS.name


def test_reconcile_declares_current_handler_shape_and_preserves_gate_evidence() -> None:
    sql = _RECONCILE.read_text(encoding="utf-8")
    for column in (
        "model_name",
        "llm_call_id",
        "prompt_text",
        "response_text",
        "tokens_to_compliance",
        "compliance_attempts",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql
    assert "quality_gates_checked_jsonb" in sql
    assert "quality_gates_failed_jsonb" in sql
    assert "jsonb_array_length" in sql
    assert "TYPE INTEGER" in sql


def test_reconcile_uses_a_bounded_maintenance_lock_and_warns_on_coercion() -> None:
    sql = _RECONCILE.read_text(encoding="utf-8")
    assert "SET LOCAL lock_timeout = '5s'" in sql
    assert "SET LOCAL statement_timeout = '2min'" in sql
    assert "LOCK TABLE delegation_events IN ACCESS EXCLUSIVE MODE" in sql
    assert "RAISE WARNING" in sql
    assert "unsupported JSONB shape" in sql


@pytest.mark.integration
async def test_real_postgres_reconciles_live_legacy_shape_idempotently() -> None:
    dsn = os.environ.get("OMN14974_POSTGRES_DSN")
    if not dsn:
        pytest.skip("OMN14974_POSTGRES_DSN not set -- skipping real Postgres proof")

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_LEGACY_SCHEMA)
        for migration in (_BASE, _METRICS, _RECONCILE, _VIEWS, _RECONCILE):
            await conn.execute(migration.read_text(encoding="utf-8"))

        columns = {
            row["column_name"]: row["data_type"]
            for row in await conn.fetch(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'delegation_events'
                """
            )
        }
        assert columns["quality_gates_checked"] == "integer"
        assert columns["quality_gates_failed"] == "integer"
        assert columns["model_name"] == "text"
        assert columns["tokens_to_compliance"] == "integer"
        assert columns["compliance_attempts"] == "integer"

        row = await conn.fetchrow(
            """
            SELECT quality_gates_checked, quality_gates_failed,
                   quality_gates_checked_jsonb, quality_gates_failed_jsonb
            FROM delegation_events
            WHERE correlation_id = 'legacy-correlation'
            """
        )
        assert row is not None
        assert row["quality_gates_checked"] == 2
        assert row["quality_gates_failed"] == 1
        assert json.loads(row["quality_gates_checked_jsonb"]) == [
            "contract",
            "quality",
        ]
        assert json.loads(row["quality_gates_failed_jsonb"]) == ["latency"]

        unsupported = await conn.fetchrow(
            """
            SELECT quality_gates_checked, quality_gates_failed,
                   quality_gates_checked_jsonb, quality_gates_failed_jsonb
            FROM delegation_events
            WHERE correlation_id = 'unsupported-correlation'
            """
        )
        assert unsupported is not None
        assert unsupported["quality_gates_checked"] == 0
        assert unsupported["quality_gates_failed"] == 0
        assert json.loads(unsupported["quality_gates_checked_jsonb"]) == {
            "contract": True
        }
        assert json.loads(unsupported["quality_gates_failed_jsonb"]) == "latency"

        await conn.execute(
            """
            INSERT INTO delegation_events (
                correlation_id, task_type, delegated_to, model_name,
                quality_gates_checked, quality_gates_failed,
                tokens_to_compliance, compliance_attempts
            ) VALUES ('current-correlation', 'summarization', 'gemini',
                      'gemini-2.5-flash-lite', 2, 0, 49, 1)
            """
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM projection_delegation_model_routing"
            )
            == 1
        )
    finally:
        await conn.close()
