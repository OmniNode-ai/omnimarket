# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Assign then read back, unknown plan refused, a second assign replaces the active row: against a scratch PostgreSQL 16."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_tenant_plan_assign_effect.handlers.handler_tenant_plan_assign import (
    HandlerTenantPlanAssign,
)
from omnimarket.nodes.node_tenant_plan_assign_effect.handlers.tenant_plan_postgres import (
    TenantPlanPostgres,
)
from omnimarket.nodes.node_tenant_plan_assign_effect.models.model_tenant_plan_assign import (
    EnumTenantPlanAssignOutcome,
    ModelTenantPlanAssignCommand,
)

from ..node_tenant_provision_effect.postgres_support import identity_tables

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OUTCOME = EnumTenantPlanAssignOutcome

_ENTITLEMENTS = """
CREATE TABLE plan_entitlements (
    plan_code text NOT NULL,
    metric_key text NOT NULL,
    window_key text NOT NULL,
    limit_value bigint,
    hard_cap boolean,
    unit text
);
INSERT INTO plan_tiers (code, name) VALUES ('build', 'Build');
INSERT INTO plan_entitlements VALUES
  ('discovery', 'workflow_submits', 'month', 1000, true, 'count'),
  ('build', 'workflow_submits', 'month', 100000, true, 'count'),
  ('build', 'requests_per_minute', 'minute', 600, false, 'count');
"""


@asynccontextmanager
async def _tables(
    port: int,
) -> AsyncIterator[tuple[asyncpg.Connection, asyncpg.Pool]]:
    async with identity_tables(port) as (admin, pool):
        await admin.execute(_ENTITLEMENTS)
        yield admin, pool


def _clock() -> datetime:
    return datetime(2026, 11, 28, 8, 0, 0, tzinfo=UTC)


def _handler(pool: asyncpg.Pool) -> HandlerTenantPlanAssign:
    return HandlerTenantPlanAssign(store=TenantPlanPostgres(pool=pool), clock=_clock)


def _command(tenant_id: UUID, plan_code: str) -> ModelTenantPlanAssignCommand:
    return ModelTenantPlanAssignCommand(
        command_id=uuid4(), tenant_id=tenant_id, plan_code=plan_code
    )


async def _tenant(admin: asyncpg.Connection, plan: str = "discovery") -> UUID:
    tenant_id: UUID = await admin.fetchval(
        "INSERT INTO tenants (tenant_slug, slug) VALUES ($1, $1) RETURNING tenant_id",
        f"t-{uuid4().hex[:8]}",
    )
    await admin.execute(
        "INSERT INTO tenant_plans (tenant_id, plan_code) VALUES ($1, $2)",
        tenant_id,
        plan,
    )
    return tenant_id


async def test_assign_then_read_back_the_entitlement(port: int) -> None:
    async with _tables(port) as (admin, pool):
        tenant_id = await _tenant(admin)
        result = await _handler(pool).handle(_command(tenant_id, "build"))
        assert result.outcome == OUTCOME.ASSIGNED
        assert (result.plan_code, result.previous_plan_code) == ("build", "discovery")
        by_metric = {e.metric_key: e for e in result.entitlements}
        assert by_metric["workflow_submits"].limit_value == 100000
        assert by_metric["workflow_submits"].window_key == "month"
        assert by_metric["workflow_submits"].hard_cap is True
        assert by_metric["requests_per_minute"].limit_value == 600
        active = await admin.fetch(
            "SELECT plan_code FROM tenant_plans WHERE tenant_id = $1 AND is_active",
            tenant_id,
        )
        assert [r["plan_code"] for r in active] == ["build"]


async def test_unknown_plan_is_refused_and_nothing_is_written(port: int) -> None:
    async with _tables(port) as (admin, pool):
        tenant_id = await _tenant(admin)
        result = await _handler(pool).handle(_command(tenant_id, "no-such-plan"))
        assert result.outcome == OUTCOME.REFUSED_UNKNOWN_PLAN
        assert result.entitlements == ()
        rows = await admin.fetch(
            "SELECT plan_code, is_active FROM tenant_plans WHERE tenant_id = $1",
            tenant_id,
        )
        assert [(r["plan_code"], r["is_active"]) for r in rows] == [("discovery", True)]


async def test_unknown_tenant_is_refused_and_nothing_is_written(port: int) -> None:
    async with _tables(port) as (admin, pool):
        result = await _handler(pool).handle(_command(uuid4(), "build"))
        assert result.outcome == OUTCOME.REFUSED_UNKNOWN_TENANT
        assert await admin.fetchval("SELECT count(*) FROM tenant_plans") == 0


async def test_a_second_assign_replaces_the_active_row(port: int) -> None:
    async with _tables(port) as (admin, pool):
        tenant_id = await _tenant(admin)
        handler = _handler(pool)
        first = await handler.handle(_command(tenant_id, "build"))
        second = await handler.handle(_command(tenant_id, "team"))
        assert (first.outcome, second.outcome) == (OUTCOME.ASSIGNED, OUTCOME.ASSIGNED)
        assert second.previous_plan_code == "build"
        rows = await admin.fetch(
            "SELECT plan_code, is_active, ended_at FROM tenant_plans "
            "WHERE tenant_id = $1 ORDER BY started_at, plan_code",
            tenant_id,
        )
        active = [r["plan_code"] for r in rows if r["is_active"]]
        assert active == ["team"]
        assert all(r["ended_at"] is not None for r in rows if not r["is_active"])
        assert {r["plan_code"] for r in rows} == {"discovery", "build", "team"}


async def test_assigning_the_active_plan_again_writes_nothing(port: int) -> None:
    async with _tables(port) as (admin, pool):
        tenant_id = await _tenant(admin, "build")
        result = await _handler(pool).handle(_command(tenant_id, "build"))
        assert result.outcome == OUTCOME.ALREADY_ASSIGNED
        assert {e.metric_key for e in result.entitlements} == {
            "workflow_submits",
            "requests_per_minute",
        }
        assert await admin.fetchval("SELECT count(*) FROM tenant_plans") == 1
