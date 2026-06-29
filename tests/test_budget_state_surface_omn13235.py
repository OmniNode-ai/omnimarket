# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13235: per-tenant ceiling budget-state surface, event-sourced.

Proves the budget-state reducer event-sources per-tenant ceiling consumption from
delegation source events: a budgeted tier draws down its monthly cap, headroom
falls, consumption rises, the surface is per-tenant + per-period, and replay is
idempotent. free_local / metered tiers produce no row (no ceiling to track).
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from omnibase_core.models.delegation.wire import EnumTierCostType, ModelTierCost

from omnimarket.nodes.node_projection_delegation.handlers.handler_budget_state import (
    TABLE,
    ModelDelegationBudgetStateEvent,
    materialize_budget_state,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_RESOLVE = (
    "omnimarket.nodes.node_projection_delegation.handlers."
    "handler_budget_state.resolve_tier_cost"
)

BUDGETED = ModelTierCost(
    cost_type=EnumTierCostType.BUDGETED,
    rate_per_1k_usd=0.01,
    monthly_cap_usd=1.0,
    overage_rate_per_1k_usd=0.05,
)
FREE_LOCAL = ModelTierCost(cost_type=EnumTierCostType.FREE_LOCAL)
METERED = ModelTierCost(cost_type=EnumTierCostType.METERED, rate_per_1k_usd=0.002)


def _event(
    *,
    correlation_id: str,
    drawdown: str = "0.0",
    overage: str = "0.0",
    tenant: str = "tenant-a",
    tier: str = "ceiling-budgeted",
    timestamp: str = "2026-06-20T12:00:00+00:00",
) -> ModelDelegationBudgetStateEvent:
    return ModelDelegationBudgetStateEvent(
        correlation_id=correlation_id,
        cost_tier_name=tier,
        budget_headroom_consumed_usd=Decimal(drawdown),
        cost_usd=Decimal(overage),
        tenant_id=tenant,
        timestamp=timestamp,
    )


@pytest.mark.unit
class TestBudgetStateEventSourcing:
    def test_consumption_accumulates_and_headroom_falls(self) -> None:
        db = InmemoryDatabaseAdapter()
        with patch(_RESOLVE, return_value=BUDGETED):
            materialize_budget_state(_event(correlation_id="c1", drawdown="0.3"), db)
            materialize_budget_state(
                _event(
                    correlation_id="c2",
                    drawdown="0.5",
                    timestamp="2026-06-20T13:00:00+00:00",
                ),
                db,
            )
        rows = db.query(TABLE, {"tenant_id": "tenant-a"})
        assert len(rows) == 1
        assert Decimal(str(rows[0]["consumed_usd"])) == Decimal("0.8")
        assert Decimal(str(rows[0]["headroom_remaining_usd"])) == Decimal("0.2")
        assert Decimal(str(rows[0]["monthly_cap_usd"])) == Decimal("1")
        assert int(rows[0]["delegation_count"]) == 2

    def test_overage_when_cap_exhausted_floors_headroom_at_zero(self) -> None:
        db = InmemoryDatabaseAdapter()
        with patch(_RESOLVE, return_value=BUDGETED):
            # Drawdown beyond the cap; overage cash recorded separately.
            materialize_budget_state(
                _event(correlation_id="c1", drawdown="1.4", overage="0.02"), db
            )
        row = db.query(TABLE, {"tenant_id": "tenant-a"})[0]
        assert Decimal(str(row["consumed_usd"])) == Decimal("1.4")
        assert Decimal(str(row["headroom_remaining_usd"])) == Decimal("0")
        assert Decimal(str(row["overage_usd"])) == Decimal("0.02")

    def test_per_tenant_isolation(self) -> None:
        db = InmemoryDatabaseAdapter()
        with patch(_RESOLVE, return_value=BUDGETED):
            materialize_budget_state(
                _event(correlation_id="a1", drawdown="0.4", tenant="tenant-a"), db
            )
            materialize_budget_state(
                _event(correlation_id="b1", drawdown="0.1", tenant="tenant-b"), db
            )
        a = db.query(TABLE, {"tenant_id": "tenant-a"})[0]
        b = db.query(TABLE, {"tenant_id": "tenant-b"})[0]
        assert Decimal(str(a["consumed_usd"])) == Decimal("0.4")
        assert Decimal(str(b["consumed_usd"])) == Decimal("0.1")

    def test_per_period_isolation(self) -> None:
        db = InmemoryDatabaseAdapter()
        with patch(_RESOLVE, return_value=BUDGETED):
            materialize_budget_state(
                _event(
                    correlation_id="m1",
                    drawdown="0.4",
                    timestamp="2026-05-31T23:00:00+00:00",
                ),
                db,
            )
            materialize_budget_state(
                _event(
                    correlation_id="m2",
                    drawdown="0.2",
                    timestamp="2026-06-01T00:00:00+00:00",
                ),
                db,
            )
        rows = db.query(TABLE, {"tenant_id": "tenant-a"})
        periods = {r["budget_period"]: Decimal(str(r["consumed_usd"])) for r in rows}
        assert periods == {"2026-05": Decimal("0.4"), "2026-06": Decimal("0.2")}

    def test_replay_is_idempotent(self) -> None:
        db = InmemoryDatabaseAdapter()
        with patch(_RESOLVE, return_value=BUDGETED):
            materialize_budget_state(_event(correlation_id="c1", drawdown="0.3"), db)
            result = materialize_budget_state(
                _event(correlation_id="c1", drawdown="0.3"), db
            )
        assert result.rows_upserted == 0
        assert result.skipped_reason == "replayed"
        row = db.query(TABLE, {"tenant_id": "tenant-a"})[0]
        assert Decimal(str(row["consumed_usd"])) == Decimal("0.3")
        assert int(row["delegation_count"]) == 1


@pytest.mark.unit
class TestNonBudgetedTiersProduceNoRow:
    def test_free_local_produces_no_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        with patch(_RESOLVE, return_value=FREE_LOCAL):
            result = materialize_budget_state(
                _event(correlation_id="c1", tier="local"), db
            )
        assert result.rows_upserted == 0
        assert result.skipped_reason == "tier_not_budgeted"
        assert db.query(TABLE, {"tenant_id": "tenant-a"}) == []

    def test_metered_produces_no_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        with patch(_RESOLVE, return_value=METERED):
            result = materialize_budget_state(
                _event(correlation_id="c1", tier="cheap_cloud"), db
            )
        assert result.rows_upserted == 0
        assert result.skipped_reason == "tier_not_budgeted"

    def test_unknown_tier_produces_no_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        with patch(_RESOLVE, return_value=None):
            result = materialize_budget_state(
                _event(correlation_id="c1", tier="nope"), db
            )
        assert result.skipped_reason == "tier_not_budgeted"


@pytest.mark.unit
class TestMigrationDeclaresTable:
    def test_migration_creates_budget_state_table(self) -> None:
        from pathlib import Path

        migration = Path(
            "src/omnimarket/nodes/node_projection_delegation/migrations/"
            "0019_delegation_budget_state.sql"
        ).read_text()
        assert "CREATE TABLE IF NOT EXISTS delegation_budget_state" in migration
        assert "ux_delegation_budget_state_identity" in migration
        assert "headroom_remaining_usd" in migration
