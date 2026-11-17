# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Own-key (tenant overlay) cost is not platform spend: the ceiling readers skip it (OMN-24874).

The delegation terminal books a tenant overlay run ``metered`` with a cost above
zero (the customer's own key paid it). The readers that sum cost into the
platform's per-tenant monthly ceiling, ``materialize_budget_state`` and its async
twin, key on the tier's registry cost model, and the routing registry declares no
``tenant_overlay`` tier, so they have always skipped it by accident of the
registry. These tests make the exclusion the readers' own rule: a registry that
declared a capped ``tenant_overlay`` tier would otherwise draw the customer's own
spend down against the platform ceiling.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from omnibase_core.models.delegation.wire import EnumTierCostType, ModelTierCost

from omnimarket.models.model_tenant_routing_overlay_backend import (
    TENANT_OVERLAY_TIER_NAME,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_budget_state import (
    TABLE,
    ModelDelegationBudgetStateEvent,
    materialize_budget_state,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_SYNC_RESOLVE = (
    "omnimarket.nodes.node_projection_delegation.handlers."
    "handler_budget_state.resolve_tier_cost"
)
_ASYNC_RESOLVE = (
    "omnimarket.nodes.node_projection_delegation.handlers."
    "handler_delegation.resolve_tier_cost"
)
# A registry that (wrongly) declares a monthly cap for every tier name it is asked about, the case the explicit rule guards.
_CAPPED = ModelTierCost(
    cost_type=EnumTierCostType.BUDGETED,
    rate_per_1k_usd=0.01,
    monthly_cap_usd=100.0,
    overage_rate_per_1k_usd=0.02,
)
_OWN_KEY_COST = Decimal("0.000145")


def _event(tier: str) -> ModelDelegationBudgetStateEvent:
    return ModelDelegationBudgetStateEvent(
        correlation_id="omn24874-own-key-run",
        cost_tier_name=tier,
        cost_measurement_source="metered",
        budget_headroom_consumed_usd=Decimal("0"),
        cost_usd=_OWN_KEY_COST,
        tenant_id="readiness-probe",
        timestamp="2026-11-16T18:00:00+00:00",
    )


@pytest.mark.unit
def test_tenant_overlay_run_never_draws_the_platform_ceiling_budget_state() -> None:
    db = InmemoryDatabaseAdapter()
    with patch(_SYNC_RESOLVE, return_value=_CAPPED):
        result = materialize_budget_state(_event(TENANT_OVERLAY_TIER_NAME), db)
    assert result.rows_upserted == 0
    assert result.skipped_reason == "tenant_overlay_not_platform_spend"
    assert db.query(TABLE, {"tenant_id": "readiness-probe"}) == []


@pytest.mark.unit
def test_a_budgeted_tier_still_draws_the_ceiling_when_the_overlay_rule_exists() -> None:
    # Positive control: the same patched registry and event shape on a real budgeted tier writes its row.
    db = InmemoryDatabaseAdapter()
    with patch(_SYNC_RESOLVE, return_value=_CAPPED):
        result = materialize_budget_state(_event("cheap_cloud"), db)
    assert result.rows_upserted == 1
    row = db.query(TABLE, {"tenant_id": "readiness-probe"})[0]
    assert Decimal(str(row["overage_usd"])) == _OWN_KEY_COST


def _run_async_twin(tier: str) -> AsyncMock:
    """Run the async ceiling reader for ``tier`` with the overlay run's own arguments and return the database mock.

    Overlay run and control share every argument (``metered``, headroom 0.0, the same cost): only the tier name differs, so a
    control that writes while the overlay run does not can only be down to the tier rule at ``handler_delegation``.
    """
    runner = DelegationProjectionRunner()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value=None)
    runner._db = db  # type: ignore[assignment]
    with patch(_ASYNC_RESOLVE, return_value=_CAPPED):
        asyncio.run(
            runner._materialize_budget_state_async(
                correlation_id="omn24874-own-key-run",
                cost_tier_name=tier,
                cost_measurement_source="metered",
                budget_headroom_consumed_usd=0.0,
                cost_usd=float(_OWN_KEY_COST),
                tenant_id="readiness-probe",
                timestamp=None,
            )
        )
    return db


@pytest.mark.unit
def test_tenant_overlay_run_never_draws_the_async_ceiling_twin() -> None:
    assert _run_async_twin(TENANT_OVERLAY_TIER_NAME).execute.await_args_list == []


@pytest.mark.unit
def test_a_budgeted_tier_still_draws_the_async_ceiling_twin() -> None:
    # Positive control: the overlay run's exact arguments with only the tier changed. The one statement is the budget upsert.
    db = _run_async_twin("cheap_cloud")
    assert len(db.execute.await_args_list) == 1
    call = db.execute.await_args_list[0]
    statement = call.args[0]
    assert "INSERT INTO delegation_budget_state AS s" in statement
    assert (
        "ON CONFLICT (tenant_id, cost_tier_name, budget_period) DO UPDATE" in statement
    )
    assert "cheap_cloud" in call.args
