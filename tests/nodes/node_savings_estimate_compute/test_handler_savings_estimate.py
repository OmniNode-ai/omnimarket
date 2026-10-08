# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_savings_estimate_compute: estimated, one definition, no baseline no figure (OMN-19979)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from omnibase_core.models.delegation.wire import EnumTierCostType, ModelTierCost
from pydantic import ValidationError

from omnimarket.models.savings_estimate import (
    EnumSavingsBasis,
    ModelClaudeCodePromptRecord,
    ModelEstimateDelegateRoute,
    ModelSavingsEstimateRow,
)
from omnimarket.nodes.node_metering_summary_compute import (
    EnumBaselineState,
    ModelCounterfactualBaseline,
    counterfactual_cost_usd,
)
from omnimarket.nodes.node_savings_estimate_compute import (
    HandlerSavingsEstimate,
    ModelSavingsEstimateRequest,
    ModelSavingsEstimateResult,
)

pytestmark = pytest.mark.unit

BASELINE = ModelCounterfactualBaseline(
    model="baseline-model",
    price_in_per_1k=Decimal("0.003"),
    price_out_per_1k=Decimal("0.015"),
    as_of="2026-09-01",
    pricing_manifest_version="1.0.0",
    source="pricing_manifest",
)
LOCAL = ModelEstimateDelegateRoute(
    tier_name="local",
    model="local-coder",
    cost=ModelTierCost(cost_type=EnumTierCostType.FREE_LOCAL),
)
METERED = ModelEstimateDelegateRoute(
    tier_name="cheap_cloud",
    model="cheap-model",
    cost=ModelTierCost(cost_type=EnumTierCostType.METERED, rate_per_1k_usd=0.5),
)


def _record(
    prompt_id: str, tokens_in: int, tokens_out: int, model: str = "claude-opus-4-6"
) -> ModelClaudeCodePromptRecord:
    return ModelClaudeCodePromptRecord(
        session_id="s1",
        prompt_id=prompt_id,
        occurred_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        assistant_messages=1 if model else 0,
    )


def _estimate(
    *records: ModelClaudeCodePromptRecord,
    baseline: ModelCounterfactualBaseline | None = BASELINE,
    route: ModelEstimateDelegateRoute = LOCAL,
) -> ModelSavingsEstimateResult:
    return HandlerSavingsEstimate().handle(
        ModelSavingsEstimateRequest(
            records=records,
            baseline_model="baseline-model",
            baseline=baseline,
            route=route,
        )
    )


def test_every_row_and_the_result_are_typed_estimated() -> None:
    result = _estimate(_record("p1", 1000, 200))
    assert result.basis is EnumSavingsBasis.ESTIMATED
    assert [row.basis for row in result.rows] == [EnumSavingsBasis.ESTIMATED]


def test_the_baseline_cost_is_the_metering_definition() -> None:
    row = _estimate(_record("p1", 1000, 200)).rows[0]
    expected = counterfactual_cost_usd(
        baseline=BASELINE, tokens_in=1000, tokens_out=200
    )
    assert row.estimated_baseline_cost_usd == expected == Decimal("0.006")
    assert row.estimated_delegate_cost_usd == Decimal("0")
    assert row.estimated_savings_usd == expected


def test_a_metered_route_is_priced_through_the_tier_cost() -> None:
    row = _estimate(_record("p1", 1000, 200), route=METERED).rows[0]
    assert row.estimated_delegate_cost_usd == Decimal("0.6")
    assert row.estimated_savings_usd == Decimal("0.006") - Decimal("0.6")
    assert (row.delegate_tier, row.delegate_model) == ("cheap_cloud", "cheap-model")


def test_totals_are_the_sum_of_the_rows() -> None:
    result = _estimate(_record("p1", 1000, 200), _record("p2", 2000, 0))
    assert result.prompts_estimated == 2
    assert result.estimated_baseline_cost_usd == Decimal("0.006") + Decimal("0.006")
    assert result.estimated_savings_usd == result.estimated_baseline_cost_usd


def test_prompts_without_usage_are_counted_and_given_no_row() -> None:
    result = _estimate(_record("p1", 1000, 200), _record("p2", 0, 0, model=""))
    assert (
        result.prompts_total,
        result.prompts_estimated,
        result.prompts_without_usage,
    ) == (2, 1, 1)
    assert [row.prompt_id for row in result.rows] == ["p1"]


def test_an_unresolved_baseline_yields_no_figure_rather_than_zero() -> None:
    result = _estimate(_record("p1", 1000, 200), baseline=None)
    assert result.baseline_state is EnumBaselineState.UNRESOLVED
    assert result.estimated_savings_usd is None
    assert result.estimated_baseline_cost_usd is None
    row = result.rows[0]
    assert row.estimated_savings_usd is None
    assert row.estimated_baseline_cost_usd is None
    assert row.baseline_model == "baseline-model"


def test_an_empty_history_has_no_figure() -> None:
    result = _estimate()
    assert result.prompts_total == 0
    assert result.estimated_savings_usd is None


def test_a_baseline_for_another_model_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelSavingsEstimateRequest(
            baseline_model="other", baseline=BASELINE, route=LOCAL
        )


def test_a_row_cannot_claim_a_saving_without_a_resolved_baseline() -> None:
    with pytest.raises(ValidationError):
        ModelSavingsEstimateRow(
            session_id="s1",
            prompt_id="p1",
            occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
            source_model="m",
            tokens_in=1,
            tokens_out=1,
            baseline_model="baseline-model",
            baseline_state=EnumBaselineState.UNRESOLVED,
            delegate_tier="local",
            delegate_model="local-coder",
            estimated_delegate_cost_usd=Decimal("0"),
            estimated_savings_usd=Decimal("0"),
        )


def test_a_row_cannot_be_typed_measured() -> None:
    row = _estimate(_record("p1", 1000, 200)).rows[0]
    with pytest.raises(ValidationError):
        ModelSavingsEstimateRow.model_validate(
            {**row.model_dump(), "basis": EnumSavingsBasis.MEASURED}
        )


def test_the_same_input_yields_the_same_output() -> None:
    records = (_record("p1", 1000, 200), _record("p2", 5, 7))
    assert _estimate(*records) == _estimate(*records)
