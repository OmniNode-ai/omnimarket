# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14894 (tranche 2): writer-boundary + RLS proof for the two tables landed.

Two tables:
  - delegation_judge_verdict_events: tenant_id resolved via a correlation_id
    join to delegation_events (same node, same migration family as
    tranche-1). The original DEFAULT_TENANT fallback (migration 0025/0026)
    was superseded by OMN-17627: an unjoinable verdict now refuses the write
    rather than stamping the house tenant.
  - projection_delegation_inference_response_text: re-keyed from a single
    global singleton to one row per tenant_id (migration 0002/0003 under
    node_projection_delegation_inference_response), closing a confirmed
    active cross-tenant leak (Linear OMN-14894 comment 6b84daf0).

Section 1/2 are unit-level, driving the actual writer seam against
InmemoryDatabaseAdapter (no DB required). Section 3 is the real-Postgres RLS
proof, mirroring tests/test_writer_tenant_isolation_omn14898.py's
_connect_or_skip pattern -- SKIPS (never ERRORs) without a reachable
database.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.events.delegation_judge_verdict import (
    EnumDelegationJudgeVerdict,
    build_delegation_judge_verdict_event,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    JUDGE_VERDICT_TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE as DELEGATION_TABLE,
)
from omnimarket.nodes.node_projection_delegation_inference_response.handlers.handler_projection_delegation_inference_response import (
    TABLE as INFERENCE_RESPONSE_TABLE,
)
from omnimarket.nodes.node_projection_delegation_inference_response.handlers.handler_projection_delegation_inference_response import (
    HandlerProjectionDelegationInferenceResponse,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.tenant_isolation import TenantRequiredError

_NODE_DIR = Path(__file__).resolve().parent.parent / "src" / "omnimarket" / "nodes"
_DELEGATION_MIGRATIONS = _NODE_DIR / "node_projection_delegation" / "migrations"
_INFERENCE_MIGRATIONS = (
    _NODE_DIR / "node_projection_delegation_inference_response" / "migrations"
)

_BASE_SQL = _DELEGATION_MIGRATIONS / "0007_delegation_events.sql"
_BUDGET_STATE_SQL = _DELEGATION_MIGRATIONS / "0019_delegation_budget_state.sql"
_TENANT_ID_SQL = _DELEGATION_MIGRATIONS / "0022_delegation_events_tenant_id.sql"
_RLS_SQL = _DELEGATION_MIGRATIONS / "0023_delegation_rls_tenant_isolation.sql"
_JUDGE_VERDICT_SQL = _DELEGATION_MIGRATIONS / "0016_delegation_judge_verdict_events.sql"
_JUDGE_VERDICT_TENANT_SQL = (
    _DELEGATION_MIGRATIONS / "0025_delegation_judge_verdict_events_tenant_id.sql"
)
_JUDGE_VERDICT_RLS_SQL = (
    _DELEGATION_MIGRATIONS
    / "0026_delegation_judge_verdict_events_rls_tenant_isolation.sql"
)
_INFERENCE_BASE_SQL = (
    _INFERENCE_MIGRATIONS
    / "0001_create_projection_delegation_inference_response_text.sql"
)
_INFERENCE_REKEY_SQL = (
    _INFERENCE_MIGRATIONS / "0002_inference_response_text_tenant_rekey.sql"
)
_INFERENCE_RLS_SQL = (
    _INFERENCE_MIGRATIONS / "0003_inference_response_text_rls_tenant_isolation.sql"
)

# Minimal, self-contained mirror of omnibase_infra forward migration
# 094_create_app_dashboard_role.sql (OMN-14899) -- see
# test_writer_tenant_isolation_omn14898.py for the same inline pattern.
_APP_DASHBOARD_ROLE_SQL = """
DO $$
BEGIN
  BEGIN
    CREATE ROLE app_dashboard WITH
      NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
  EXCEPTION
    WHEN duplicate_object OR unique_violation THEN
      NULL;
  END;
END;
$$;
ALTER ROLE app_dashboard
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
"""


def _build_verdict(correlation_id: object, *, task_type: str = "research") -> object:
    return build_delegation_judge_verdict_event(
        correlation_id=correlation_id,  # type: ignore[arg-type]
        task_type=task_type,
        judge_model="glm-5.2",
        judge_model_version="v1",
        judge_provider="zai",
        rubric_id="rubric-1",
        rubric_hash="sha256:" + "a" * 64,
        prompt="prompt text",
        judged_input="input text",
        temperature=0.0,
        judge_node_version="1.0.0",
        reasoning="reasoning text",
        verdict=EnumDelegationJudgeVerdict.PASS,
        actual_score=0.9,
    )


# ---------------------------------------------------------------------------
# 1. delegation_judge_verdict_events: tenant_id resolved via correlation_id
#    join to delegation_events, defaulting when unmatched.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_judge_verdict_resolves_tenant_from_matching_delegation_event() -> None:
    """A judge verdict whose correlation_id matches a delegation_events row
    inherits that row's tenant_id."""
    db = InmemoryDatabaseAdapter()
    correlation_id = uuid4()
    db.upsert(
        DELEGATION_TABLE,
        "correlation_id",
        {"correlation_id": str(correlation_id), "tenant_id": "tenant-a"},
    )

    handler = HandlerProjectionDelegation()
    event = _build_verdict(correlation_id)
    result = handler.project_judge_verdict(event, db)  # type: ignore[arg-type]

    assert result.rows_upserted == 1
    rows = db.query(JUDGE_VERDICT_TABLE)
    assert rows[0]["tenant_id"] == "tenant-a"


@pytest.mark.unit
def test_judge_verdict_refuses_when_unmatched() -> None:
    """No matching delegation_events row -> refuse the write (OMN-17627).

    SUPERSEDES this file's original assertion, which required DEFAULT_TENANT
    here so the row was "never silently tenant-less". OMN-16831/OMN-16804
    ratified the stronger rule -- attribution is producer-recorded or
    verified, never invented -- and a house-stamped row is an invented one.

    Both goals hold under refusal: the async writer routes a refused verdict
    to the contract-declared DLQ, so it is neither invented nor silently lost.

    Still mirrors the live join-completeness gap this ticket surfaced (2 of 4
    stability-test rows had no matching delegation_events correlation_id) --
    what changed is that the gap now refuses instead of absorbing.
    """
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegation()
    event = _build_verdict(uuid4())

    with pytest.raises(TenantRequiredError):
        handler.project_judge_verdict(event, db)  # type: ignore[arg-type]

    assert db.query(JUDGE_VERDICT_TABLE) == []


# ---------------------------------------------------------------------------
# 2. projection_delegation_inference_response_text: per-tenant re-key.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_inference_response_two_tenants_do_not_collapse() -> None:
    """Cross-boundary proof of the 6b84daf0 leak closing: two tenants writing
    through the actual handler produce two rows, not one collapsed row."""
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegationInferenceResponse()

    handler.project(
        {
            "correlation_id": str(uuid4()),
            "content": "tenant-a text",
            "model_used": "glm-5.2",
            "tenant_id": "tenant-a",
        },
        db,  # type: ignore[arg-type]
    )
    handler.project(
        {
            "correlation_id": str(uuid4()),
            "content": "tenant-b text",
            "model_used": "glm-5.2",
            "tenant_id": "tenant-b",
        },
        db,  # type: ignore[arg-type]
    )

    rows = db.query(INFERENCE_RESPONSE_TABLE)
    assert len(rows) == 2
    texts = {row["tenant_id"]: row["latest_generated_text"] for row in rows}
    assert texts == {"tenant-a": "tenant-a text", "tenant-b": "tenant-b text"}


# ---------------------------------------------------------------------------
# 3. Real-Postgres RLS cross-tenant isolation proof (skips without a live DB).
# ---------------------------------------------------------------------------


def _sync_dsn_for_schema(schema: str) -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    options = quote(f"-c search_path={schema},public")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
        f"?options={options}"
    )


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("POSTGRES_PASSWORD not set -- skipping RLS isolation DB proof")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
        pytest.skip(f"no reachable Postgres for RLS isolation DB proof: {exc}")


@pytest.mark.integration
async def test_real_postgres_tranche2_rls_isolates_both_tables() -> None:
    """Apply tranche-1 + tranche-2 migrations for both tables, then prove
    tenant A cannot read tenant B's row on either surface."""
    conn = await _connect_or_skip()
    schema = "omn14894_tranche2_test"

    await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await conn.execute(f"CREATE SCHEMA {schema}")
    try:
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute(_BASE_SQL.read_text(encoding="utf-8"))
        await conn.execute(_BUDGET_STATE_SQL.read_text(encoding="utf-8"))
        await conn.execute(_TENANT_ID_SQL.read_text(encoding="utf-8"))
        await conn.execute(_JUDGE_VERDICT_SQL.read_text(encoding="utf-8"))
        await conn.execute(_APP_DASHBOARD_ROLE_SQL)
        await conn.execute(_RLS_SQL.read_text(encoding="utf-8"))
        await conn.execute(_JUDGE_VERDICT_TENANT_SQL.read_text(encoding="utf-8"))
        await conn.execute(_JUDGE_VERDICT_RLS_SQL.read_text(encoding="utf-8"))
        await conn.execute(_INFERENCE_BASE_SQL.read_text(encoding="utf-8"))
        await conn.execute(_INFERENCE_REKEY_SQL.read_text(encoding="utf-8"))
        await conn.execute(_INFERENCE_RLS_SQL.read_text(encoding="utf-8"))
        await conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO app_dashboard")
        await conn.execute(
            "ALTER ROLE app_dashboard LOGIN PASSWORD 'omn14894-test-only-throwaway'"
        )

        adapter = PostgresSyncProjectionAdapter(_sync_dsn_for_schema(schema))
        adapter.upsert(
            JUDGE_VERDICT_TABLE,
            "event_hash",
            {
                "event_hash": "sha256:" + "b" * 64,
                "correlation_id": "omn14894-tenant-a",
                "task_type": "research",
                "score_source": "reproducible_judge",
                "judge_model": "glm-5.2",
                "judge_model_version": "v1",
                "judge_provider": "zai",
                "rubric_id": "rubric-1",
                "rubric_hash": "sha256:" + "a" * 64,
                "prompt_hash": "sha256:" + "c" * 64,
                "input_hash": "sha256:" + "d" * 64,
                "temperature": 0.0,
                "judge_node_version": "1.0.0",
                "reasoning_hash": "sha256:" + "e" * 64,
                "verdict": "pass",
                "actual_score": 0.9,
                "tenant_id": "tenant-a",
            },
        )
        adapter.upsert(
            JUDGE_VERDICT_TABLE,
            "event_hash",
            {
                "event_hash": "sha256:" + "f" * 64,
                "correlation_id": "omn14894-tenant-b",
                "task_type": "research",
                "score_source": "reproducible_judge",
                "judge_model": "glm-5.2",
                "judge_model_version": "v1",
                "judge_provider": "zai",
                "rubric_id": "rubric-1",
                "rubric_hash": "sha256:" + "a" * 64,
                "prompt_hash": "sha256:" + "c" * 64,
                "input_hash": "sha256:" + "d" * 64,
                "temperature": 0.0,
                "judge_node_version": "1.0.0",
                "reasoning_hash": "sha256:" + "e" * 64,
                "verdict": "pass",
                "actual_score": 0.5,
                "tenant_id": "tenant-b",
            },
        )
        adapter.upsert(
            INFERENCE_RESPONSE_TABLE,
            "singleton_key",
            {
                "singleton_key": "tenant-a",
                "tenant_id": "tenant-a",
                "latest_generated_text": "tenant-a inference text",
            },
        )
        adapter.upsert(
            INFERENCE_RESPONSE_TABLE,
            "singleton_key",
            {
                "singleton_key": "tenant-b",
                "tenant_id": "tenant-b",
                "latest_generated_text": "tenant-b inference text",
            },
        )

        reader_dsn = (
            "postgresql://app_dashboard:omn14894-test-only-throwaway@"
            f"{os.environ.get('INTEGRATION_POSTGRES_HOST', 'localhost')}:"
            f"{os.environ.get('INTEGRATION_POSTGRES_PORT', '5432')}/"
            f"{os.environ.get('INTEGRATION_POSTGRES_DB', 'omnibase_infra')}"
        )
        reader = await asyncpg.connect(reader_dsn)
        try:
            await reader.execute(f"SET search_path TO {schema}, public")

            await reader.execute("SET app.tenant_id = 'tenant-a'")
            jv_as_a = await reader.fetch(
                f"SELECT tenant_id FROM {JUDGE_VERDICT_TABLE} ORDER BY tenant_id"
            )
            ir_as_a = await reader.fetch(
                f"SELECT tenant_id FROM {INFERENCE_RESPONSE_TABLE} ORDER BY tenant_id"
            )

            await reader.execute("SET app.tenant_id = 'tenant-b'")
            jv_as_b = await reader.fetch(
                f"SELECT tenant_id FROM {JUDGE_VERDICT_TABLE} ORDER BY tenant_id"
            )
            ir_as_b = await reader.fetch(
                f"SELECT tenant_id FROM {INFERENCE_RESPONSE_TABLE} ORDER BY tenant_id"
            )
        finally:
            await reader.close()

        assert [dict(r) for r in jv_as_a] == [{"tenant_id": "tenant-a"}]
        assert [dict(r) for r in jv_as_b] == [{"tenant_id": "tenant-b"}]
        assert [dict(r) for r in ir_as_a] == [{"tenant_id": "tenant-a"}]
        assert [dict(r) for r in ir_as_b] == [{"tenant_id": "tenant-b"}]
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()
