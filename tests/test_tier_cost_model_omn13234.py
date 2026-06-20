# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13234: typed per-tier cost model — computation + routing_tiers parse.

DoD coverage:
  - metered            -> rate * tokens
  - budgeted under cap -> headroom decremented, cash 0
  - budgeted over cap  -> overage rate applied
  - free_local         -> 0
plus: routing_tiers.yaml declares a typed `cost` block for every tier and the
parser round-trips it into the canonical ModelTierCost.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.models.delegation.wire import EnumTierCostType, ModelTierCost

from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)
from omnimarket.pricing import compute_tier_cost_usd


@pytest.mark.unit
class TestComputeTierCostUsd:
    def test_free_local_is_zero(self) -> None:
        cost = ModelTierCost(cost_type=EnumTierCostType.FREE_LOCAL)
        result = compute_tier_cost_usd(
            cost=cost, prompt_tokens=5000, completion_tokens=5000
        )
        assert result.cash_cost_usd == 0.0
        assert result.headroom_consumed_usd == 0.0
        assert result.measurement_source == "free_local"

    def test_metered_is_rate_times_tokens(self) -> None:
        cost = ModelTierCost(cost_type=EnumTierCostType.METERED, rate_per_1k_usd=0.002)
        # 10_000 tokens * 0.002 / 1000 = 0.02
        result = compute_tier_cost_usd(
            cost=cost, prompt_tokens=6000, completion_tokens=4000
        )
        assert result.cash_cost_usd == pytest.approx(0.02)
        assert result.headroom_consumed_usd == 0.0
        assert result.measurement_source == "metered"

    def test_budgeted_under_cap_decrements_headroom_zero_cash(self) -> None:
        cost = ModelTierCost(
            cost_type=EnumTierCostType.BUDGETED,
            rate_per_1k_usd=0.001,
            monthly_cap_usd=10.0,
            overage_rate_per_1k_usd=0.003,
        )
        # 10_000 tokens * 0.001 / 1000 = 0.01 accounting cost; headroom 5.0 covers it.
        result = compute_tier_cost_usd(
            cost=cost,
            prompt_tokens=6000,
            completion_tokens=4000,
            remaining_budget_usd=5.0,
        )
        assert result.cash_cost_usd == 0.0
        assert result.headroom_consumed_usd == pytest.approx(0.01)
        assert result.measurement_source == "budgeted_in_budget"

    def test_budgeted_over_cap_applies_overage(self) -> None:
        cost = ModelTierCost(
            cost_type=EnumTierCostType.BUDGETED,
            rate_per_1k_usd=0.001,
            monthly_cap_usd=10.0,
            overage_rate_per_1k_usd=0.003,
        )
        # No headroom left -> every token billed at overage 0.003.
        # 10_000 tokens * 0.003 / 1000 = 0.03
        result = compute_tier_cost_usd(
            cost=cost,
            prompt_tokens=6000,
            completion_tokens=4000,
            remaining_budget_usd=0.0,
        )
        assert result.cash_cost_usd == pytest.approx(0.03)
        assert result.headroom_consumed_usd == 0.0
        assert result.measurement_source == "budgeted_overage"

    def test_budgeted_split_partial_headroom(self) -> None:
        cost = ModelTierCost(
            cost_type=EnumTierCostType.BUDGETED,
            rate_per_1k_usd=0.001,
            monthly_cap_usd=10.0,
            overage_rate_per_1k_usd=0.003,
        )
        # 10_000 tokens accounting cost 0.01; only 0.005 headroom -> ~5000 tokens
        # in-budget (0 cash, 0.005 drawdown), ~5000 overage at 0.003 -> 0.015.
        result = compute_tier_cost_usd(
            cost=cost,
            prompt_tokens=6000,
            completion_tokens=4000,
            remaining_budget_usd=0.005,
        )
        assert result.measurement_source == "budgeted_split"
        assert result.headroom_consumed_usd == pytest.approx(0.005)
        assert result.cash_cost_usd == pytest.approx(0.015)

    def test_budgeted_unknown_headroom_is_full_overage(self) -> None:
        cost = ModelTierCost(
            cost_type=EnumTierCostType.BUDGETED,
            rate_per_1k_usd=0.001,
            monthly_cap_usd=10.0,
            overage_rate_per_1k_usd=0.003,
        )
        # remaining_budget_usd None -> conservative: treat as exhausted.
        result = compute_tier_cost_usd(
            cost=cost,
            prompt_tokens=6000,
            completion_tokens=4000,
            remaining_budget_usd=None,
        )
        assert result.cash_cost_usd == pytest.approx(0.03)
        assert result.measurement_source == "budgeted_overage"

    def test_none_cost_model_falls_through(self) -> None:
        result = compute_tier_cost_usd(
            cost=None, prompt_tokens=1000, completion_tokens=1000
        )
        assert result.cash_cost_usd == 0.0
        assert result.measurement_source == "no_cost_model"


@pytest.mark.unit
class TestRoutingTiersTypedCost:
    def _config(self) -> object:
        config_path = Path("src/omnimarket/configs/routing_tiers.yaml")
        return parse_delegation_config_yaml(config_path.read_text(encoding="utf-8"))

    def test_every_tier_declares_typed_cost(self) -> None:
        config = self._config()
        for tier in config.tiers:  # type: ignore[attr-defined]
            assert tier.cost is not None, f"tier {tier.name} missing typed cost"

    def test_local_tier_is_free_local(self) -> None:
        config = self._config()
        by_name = {t.name: t for t in config.tiers}  # type: ignore[attr-defined]
        assert by_name["local"].cost.cost_type is EnumTierCostType.FREE_LOCAL

    def test_cheap_cloud_tier_is_metered(self) -> None:
        config = self._config()
        by_name = {t.name: t for t in config.tiers}  # type: ignore[attr-defined]
        cheap = by_name["cheap_cloud"].cost
        assert cheap.cost_type is EnumTierCostType.METERED
        assert cheap.rate_per_1k_usd == pytest.approx(0.002)

    def test_ceiling_tier_is_free_local(self) -> None:
        """OMN-13351: ceiling routes to free-tier Gemini -> zero marginal cost."""
        config = self._config()
        by_name = {t.name: t for t in config.tiers}  # type: ignore[attr-defined]
        assert by_name["claude"].cost.cost_type is EnumTierCostType.FREE_LOCAL
