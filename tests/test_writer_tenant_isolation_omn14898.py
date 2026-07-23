# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14898: writer-boundary fail-closed tenant isolation, cross-boundary proof.

Ground truth this test pins (see ``omnimarket.projection.tenant_isolation`` for
the full rationale): the envelope-side tenant stamp is already canonical
(OMN-14208) and DB-side RLS is already landed (migration 0023, OMN-14894
tranche 1). The remaining gap this ticket closes is that
``HandlerProjectionDelegation``/``materialize_budget_state`` silently OMIT the
``tenant_id`` key when the source event carries none (OMN-14058,
OPERATOR-ACCEPTED INTERIM) -- Postgres' column default then absorbs the write
instead of refusing it. ``ENFORCE_TENANT_ISOLATION`` (default False, see
Settings) makes that refusal real without breaking the OMN-14058 single-tenant
interim that every lane runs under today.

This module drives the ACTUAL writer seam end-to-end -- not two independent
unit suites (the OMN-14208 near-miss this ticket explicitly calls out):

  event (tenant_id=X | blank)
    -> HandlerProjectionDelegation.project() / .handle()
    -> delegation_events row (X) | TenantRequiredError, zero rows
    -> (real Postgres) RLS: SET app.tenant_id=A cannot read a row stamped B

Section 3 is the real-Postgres proof; it SKIPS (never ERRORs) without a
reachable database, mirroring
``test_delegation_savings_tenant_id_column_omn14058.py``'s ``_connect_or_skip``.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, quote_plus
from uuid import uuid4

import asyncpg
import pytest
from omnibase_core.models.delegation.wire import EnumTierCostType, ModelTierCost

from omnimarket.config.settings import Settings
from omnimarket.nodes.node_projection_delegation.handlers.handler_budget_state import (
    DEFAULT_TENANT,
    ModelDelegationBudgetStateEvent,
    materialize_budget_state,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE as DELEGATION_TABLE,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.projection import tenant_isolation as tenant_isolation_module
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.tenant_isolation import (
    TenantRequiredError,
    require_tenant_id,
)

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)
_BASE_SQL = _MIGRATIONS_DIR / "0007_delegation_events.sql"
_BUDGET_STATE_SQL = _MIGRATIONS_DIR / "0019_delegation_budget_state.sql"
_TENANT_ID_SQL = _MIGRATIONS_DIR / "0022_delegation_events_tenant_id.sql"
_RLS_SQL = _MIGRATIONS_DIR / "0023_delegation_rls_tenant_isolation.sql"

# Minimal, self-contained mirror of omnibase_infra forward migration
# 094_create_app_dashboard_role.sql (OMN-14899) -- role creation is
# cluster-wide and cross-repo; inlining the guarded CREATE keeps this test
# runnable from the omnimarket tree alone rather than depending on a sibling
# repo checkout being present at a specific relative path.
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


def _enable_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tenant_isolation_module,
        "get_settings",
        lambda: Settings(enforce_tenant_isolation=True),
    )


def _disable_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tenant_isolation_module,
        "get_settings",
        lambda: Settings(enforce_tenant_isolation=False),
    )


def _budgeted_tier() -> ModelTierCost:
    return ModelTierCost(
        cost_type=EnumTierCostType.BUDGETED,
        rate_per_1k_usd=0.01,
        monthly_cap_usd=1.0,
        overage_rate_per_1k_usd=0.05,
    )


# ---------------------------------------------------------------------------
# 1. require_tenant_id guard -- unit level, no DB.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_require_tenant_id_noop_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (ENFORCE_TENANT_ISOLATION=False) never raises -- OMN-14058 intact."""
    _disable_enforcement(monkeypatch)
    require_tenant_id(None, table="delegation_events")
    require_tenant_id("", table="delegation_events")
    require_tenant_id("   ", table="delegation_events")


@pytest.mark.unit
def test_require_tenant_id_raises_when_enforced_and_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_enforcement(monkeypatch)
    with pytest.raises(TenantRequiredError):
        require_tenant_id(None, table="delegation_events")
    with pytest.raises(TenantRequiredError):
        require_tenant_id("", table="delegation_events")
    with pytest.raises(TenantRequiredError):
        require_tenant_id("   ", table="delegation_events")


@pytest.mark.unit
def test_require_tenant_id_passes_when_enforced_and_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_enforcement(monkeypatch)
    require_tenant_id("acme-corp", table="delegation_events")


# ---------------------------------------------------------------------------
# 2. Writer boundary (HandlerProjectionDelegation / materialize_budget_state)
#    -- the actual seam, driven end-to-end against InmemoryDatabaseAdapter.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_writer_refuses_missing_tenant_and_never_produces_a_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: a tenant-less write raises and leaves zero rows behind."""
    _enable_enforcement(monkeypatch)

    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegation()
    event = ModelTaskDelegatedEvent(
        correlation_id=str(uuid4()),
        task_type="test",
        delegated_to="local-runtime",
        tenant_id=None,
    )

    with pytest.raises(TenantRequiredError):
        handler.project(event, db)

    assert db.query(DELEGATION_TABLE) == [], (
        "a refused write must never produce a projection row"
    )


@pytest.mark.unit
def test_writer_accepts_valid_tenant_and_stamps_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-formed tenant_id still writes normally under enforcement."""
    _enable_enforcement(monkeypatch)

    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegation()
    event = ModelTaskDelegatedEvent(
        correlation_id=str(uuid4()),
        task_type="test",
        delegated_to="local-runtime",
        tenant_id="acme-corp",
    )

    result = handler.project(event, db)

    assert result.rows_upserted == 1
    rows = db.query(DELEGATION_TABLE)
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == "acme-corp"


@pytest.mark.unit
def test_budget_state_writer_refuses_missing_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: a tenant-less budget event raises and leaves zero rows."""
    _enable_enforcement(monkeypatch)

    db = InmemoryDatabaseAdapter()
    event = ModelDelegationBudgetStateEvent(
        correlation_id="c-omn14898",
        cost_tier_name="ceiling-budgeted",
        budget_headroom_consumed_usd="0.10",
        cost_usd="0.0",
        tenant_id=None,
    )
    # Patch resolve_tier_cost to a budgeted tier so the guard is reached
    # (a non-budgeted tier short-circuits before the tenant check).
    with (
        patch(
            "omnimarket.nodes.node_projection_delegation.handlers."
            "handler_budget_state.resolve_tier_cost",
            return_value=_budgeted_tier(),
        ),
        pytest.raises(TenantRequiredError),
    ):
        materialize_budget_state(event, db)

    assert db.query("delegation_budget_state") == []


@pytest.mark.unit
def test_budget_state_default_tenant_unchanged_without_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-14058's DEFAULT_TENANT fallback is unchanged with enforcement off."""
    _disable_enforcement(monkeypatch)

    db = InmemoryDatabaseAdapter()
    event = ModelDelegationBudgetStateEvent(
        correlation_id="c-omn14898-default",
        cost_tier_name="ceiling-budgeted",
        budget_headroom_consumed_usd="0.10",
        cost_usd="0.0",
        tenant_id=None,
    )
    with patch(
        "omnimarket.nodes.node_projection_delegation.handlers."
        "handler_budget_state.resolve_tier_cost",
        return_value=_budgeted_tier(),
    ):
        result = materialize_budget_state(event, db)

    assert result.rows_upserted == 1
    rows = db.query("delegation_budget_state")
    assert rows[0]["tenant_id"] == DEFAULT_TENANT


# ---------------------------------------------------------------------------
# 3. Real-Postgres RLS cross-tenant isolation proof (skips without a live DB).
# ---------------------------------------------------------------------------


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
    except (
        OSError,
        asyncpg.PostgresError,
    ) as exc:  # pragma: no cover - infra-dependent
        pytest.skip(f"no reachable Postgres for RLS isolation DB proof: {exc}")


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


@pytest.mark.integration
async def test_real_postgres_cross_tenant_write_is_rls_isolated_on_read() -> None:
    """Apply 0007+0019+0022+0023, then prove tenant A cannot read tenant B's row.

    Writers on compose lanes connect as the ``postgres`` SUPERUSER and bypass
    RLS regardless of this ticket's application-level guard (see
    ``migrations/0023``'s own BLAST RADIUS note) -- the isolation boundary
    proven here is the RLS policy against the constrained ``app_dashboard``
    READ role, exactly the boundary OMN-14894/OMN-14899 landed. This test
    grants ``app_dashboard`` a throwaway, test-local LOGIN password scoped to
    the disposable fixture schema only; it does not touch any live credential
    or Secrets Manager wiring (that live role/credential conversion for the
    WRITE path is the Daniyal-owned handoff -- see the PR body).
    """
    conn = await _connect_or_skip()
    schema = "omn14898_writer_isolation_test"

    await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await conn.execute(f"CREATE SCHEMA {schema}")
    try:
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute(_BASE_SQL.read_text(encoding="utf-8"))
        await conn.execute(_BUDGET_STATE_SQL.read_text(encoding="utf-8"))
        await conn.execute(_TENANT_ID_SQL.read_text(encoding="utf-8"))
        await conn.execute(_APP_DASHBOARD_ROLE_SQL)
        await conn.execute(_RLS_SQL.read_text(encoding="utf-8"))
        await conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO app_dashboard")
        # Test-local throwaway login credential, scoped to this disposable
        # schema/session only -- never a live/production credential.
        await conn.execute(
            "ALTER ROLE app_dashboard LOGIN PASSWORD 'omn14898-test-only-throwaway'"
        )

        adapter = PostgresSyncProjectionAdapter(_sync_dsn_for_schema(schema))
        adapter.upsert(
            DELEGATION_TABLE,
            "correlation_id",
            {"correlation_id": "omn14898-tenant-a", "tenant_id": "tenant-a"},
        )
        adapter.upsert(
            DELEGATION_TABLE,
            "correlation_id",
            {"correlation_id": "omn14898-tenant-b", "tenant_id": "tenant-b"},
        )

        reader_dsn = (
            "postgresql://app_dashboard:omn14898-test-only-throwaway@"
            f"{os.environ.get('INTEGRATION_POSTGRES_HOST', 'localhost')}:"
            f"{os.environ.get('INTEGRATION_POSTGRES_PORT', '5432')}/"
            f"{os.environ.get('INTEGRATION_POSTGRES_DB', 'omnibase_infra')}"
        )
        reader = await asyncpg.connect(reader_dsn)
        try:
            await reader.execute(f"SET search_path TO {schema}, public")
            await reader.execute("SET app.tenant_id = 'tenant-a'")
            visible_as_a = await reader.fetch(
                "SELECT correlation_id, tenant_id FROM delegation_events "
                "ORDER BY correlation_id"
            )
            await reader.execute("SET app.tenant_id = 'tenant-b'")
            visible_as_b = await reader.fetch(
                "SELECT correlation_id, tenant_id FROM delegation_events "
                "ORDER BY correlation_id"
            )
        finally:
            await reader.close()

        assert [dict(r) for r in visible_as_a] == [
            {"correlation_id": "omn14898-tenant-a", "tenant_id": "tenant-a"}
        ], "tenant-a session must see only its own row, never tenant-b's"
        assert [dict(r) for r in visible_as_b] == [
            {"correlation_id": "omn14898-tenant-b", "tenant_id": "tenant-b"}
        ], "tenant-b session must see only its own row, never tenant-a's"
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()
