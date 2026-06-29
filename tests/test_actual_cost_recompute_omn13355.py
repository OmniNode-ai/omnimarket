# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13355: measured actual cost wired into cost_usd + savings provenance.

Proves the projection recomputes cost_usd as a MEASUREMENT — the serving tier's
typed cost model (OMN-13234) priced against the measured tokens — instead of
persisting the workflow handler's hardcoded 0.0, so the saving is
``premium_counterfactual - real_actual`` rather than ``counterfactual - 0``. Also
proves the recompute validator asserts actual-cost provenance.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from omnibase_core.models.delegation.wire import EnumTierCostType

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
    validate_actual_cost_provenance,
)
from omnimarket.pricing import (
    build_premium_counterfactual,
    recompute_actual_cost_and_savings,
    resolve_tier_cost,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionDelegation()


@pytest.mark.unit
class TestResolveTierCost:
    def test_cheap_cloud_is_metered(self) -> None:
        cost = resolve_tier_cost("cheap_cloud")
        assert cost is not None
        assert cost.cost_type is EnumTierCostType.METERED
        assert cost.rate_per_1k_usd == pytest.approx(0.002)

    def test_local_is_free_local(self) -> None:
        cost = resolve_tier_cost("local")
        assert cost is not None
        assert cost.cost_type is EnumTierCostType.FREE_LOCAL

    def test_unknown_tier_is_none(self) -> None:
        assert resolve_tier_cost("tier-that-does-not-exist") is None


@pytest.mark.unit
class TestRecomputeActualCostAndSavings:
    def test_metered_saving_is_counterfactual_minus_real_actual(self) -> None:
        # 1500 tokens @ 0.002/1k = 0.003 real actual; the saving must subtract it.
        cf = build_premium_counterfactual(prompt_tokens=1000, completion_tokens=500)
        assert cf is not None
        m = recompute_actual_cost_and_savings(
            tier_name="cheap_cloud",
            prompt_tokens=1000,
            completion_tokens=500,
            premium_counterfactual=cf,
        )
        assert m.cash_cost_usd == pytest.approx(0.003)
        assert m.cost_measurement_source == "metered"
        # The bug being closed: saving is NOT the full counterfactual (0.0525).
        assert m.cost_savings_usd != float(cf.counterfactual_cost_usd)
        assert m.cost_savings_usd == pytest.approx(0.0495)

    def test_free_local_actual_is_zero_full_counterfactual_saved(self) -> None:
        cf = build_premium_counterfactual(prompt_tokens=1000, completion_tokens=500)
        assert cf is not None
        m = recompute_actual_cost_and_savings(
            tier_name="local",
            prompt_tokens=1000,
            completion_tokens=500,
            premium_counterfactual=cf,
        )
        assert m.cash_cost_usd == 0.0
        assert m.cost_measurement_source == "free_local"
        # Free local has zero marginal cost, so the full counterfactual is saved.
        assert m.cost_savings_usd == pytest.approx(float(cf.counterfactual_cost_usd))

    def test_no_counterfactual_yields_zero_saving_with_provenance(self) -> None:
        m = recompute_actual_cost_and_savings(
            tier_name="cheap_cloud",
            prompt_tokens=1000,
            completion_tokens=500,
            premium_counterfactual=None,
        )
        assert m.cost_savings_usd == 0.0
        # Actual-cost measurement still produced even with no baseline.
        assert m.cash_cost_usd == pytest.approx(0.003)
        assert m.cost_measurement_source == "metered"


@pytest.mark.unit
class TestProjectionWiresMeasuredActualCost:
    def test_metered_row_persists_measured_cost_not_zero(self) -> None:
        db = InmemoryDatabaseAdapter()
        cf = build_premium_counterfactual(prompt_tokens=1000, completion_tokens=500)
        assert cf is not None
        # The durable event carries cost_usd=0.0 (the workflow-handler bug); the
        # projection must OVERRIDE it with the measured tier cost.
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-actual-metered",
            task_type="code_generation",
            delegated_to="cheap-cloud-glm",
            quality_gate_passed=True,
            cost_usd=0.0,
            cost_savings_usd=0.0525,
            tokens_input=1000,
            tokens_output=500,
            cost_tier_name="cheap_cloud",
            premium_counterfactual=cf,
        )
        assert HANDLER.project(event, db).rows_upserted == 1
        row = db.query("delegation_events", {"correlation_id": "corr-actual-metered"})[
            0
        ]
        # cost_usd is now the measurement, not the hardcoded 0.0.
        assert Decimal(str(row["cost_usd"])) == Decimal("0.003")
        assert row["cost_measurement_source"] == "metered"
        assert row["cost_tier_type"] == "metered"
        # Saving is counterfactual - real_actual.
        assert Decimal(str(row["cost_savings_usd"])) == Decimal("0.0495")
        validate_actual_cost_provenance(row)

    def test_free_local_row_full_counterfactual_saved(self) -> None:
        db = InmemoryDatabaseAdapter()
        cf = build_premium_counterfactual(prompt_tokens=1000, completion_tokens=500)
        assert cf is not None
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-actual-local",
            task_type="code_generation",
            delegated_to="local-qwen",
            quality_gate_passed=True,
            cost_usd=0.0,
            cost_savings_usd=0.0,
            tokens_input=1000,
            tokens_output=500,
            cost_tier_name="local",
            premium_counterfactual=cf,
        )
        assert HANDLER.project(event, db).rows_upserted == 1
        row = db.query("delegation_events", {"correlation_id": "corr-actual-local"})[0]
        assert Decimal(str(row["cost_usd"])) == Decimal("0")
        assert row["cost_measurement_source"] == "free_local"
        assert Decimal(str(row["cost_savings_usd"])) == cf.counterfactual_cost_usd
        validate_actual_cost_provenance(row)

    def test_no_tier_name_preserves_event_values(self) -> None:
        # Backward-compatible fall-through: a row without a serving tier keeps the
        # event's own cost/savings (e.g. legacy zero-token golden-chain rows).
        db = InmemoryDatabaseAdapter()
        event = ModelTaskDelegatedEvent(
            correlation_id="corr-no-tier",
            task_type="code-review",
            delegated_to="local-qwen",
            quality_gate_passed=True,
            cost_usd=0.0,
            cost_savings_usd=0.0,
            tokens_input=0,
            tokens_output=0,
        )
        assert HANDLER.project(event, db).rows_upserted == 1
        row = db.query("delegation_events", {"correlation_id": "corr-no-tier"})[0]
        assert Decimal(str(row["cost_usd"])) == Decimal("0")
        assert Decimal(str(row["cost_savings_usd"])) == Decimal("0")
        validate_actual_cost_provenance(row)


@pytest.mark.unit
class TestRecomputeValidator:
    def test_rejects_nonzero_saving_without_provenance(self) -> None:
        with pytest.raises(ValueError, match="no cost_measurement_source"):
            validate_actual_cost_provenance(
                {
                    "cost_savings_usd": 0.05,
                    "cost_usd": 0.0,
                    "cost_measurement_source": "",
                }
            )

    def test_rejects_savings_that_does_not_reconcile(self) -> None:
        with pytest.raises(ValueError, match="does not reconcile"):
            validate_actual_cost_provenance(
                {
                    "cost_savings_usd": 0.05,
                    "cost_usd": 0.003,
                    "cost_measurement_source": "metered",
                    "premium_counterfactual": {"counterfactual_cost_usd": "0.0525"},
                }
            )

    def test_accepts_reconciling_row(self) -> None:
        # 0.0525 - 0.003 == 0.0495 — reconciles.
        validate_actual_cost_provenance(
            {
                "cost_savings_usd": 0.0495,
                "cost_usd": 0.003,
                "cost_measurement_source": "metered",
                "premium_counterfactual": {"counterfactual_cost_usd": "0.0525"},
            }
        )

    def test_zero_saving_with_empty_source_is_allowed(self) -> None:
        # A truthful zero-saving row (no delegation cost benefit) needs no source.
        validate_actual_cost_provenance(
            {"cost_savings_usd": 0.0, "cost_usd": 0.0, "cost_measurement_source": ""}
        )
