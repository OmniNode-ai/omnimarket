# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerAbCompareReducer.

All tests are pure — no I/O, no network. The reducer is a pure function.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_ab_compare_reducer.handlers.handler_ab_compare_reducer import (
    HandlerAbCompareReducer,
    PricingMap,
)
from omnimarket.nodes.node_ab_compare_reducer.models.model_ab_compare_state import (
    ModelAbCompareState,
)
from omnimarket.nodes.node_ab_compare_reducer.models.model_inference_result_entry import (
    ModelInferenceResultEntry,
)

CORR_ID = "test-corr-abc123"

PRICING: PricingMap = {
    "local-model": {
        "display_name": "Local Model",
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
    },
    "cloud-model": {
        "display_name": "Cloud Model",
        "cost_per_1k_input": 0.003,
        "cost_per_1k_output": 0.015,
    },
    "mid-cloud-model": {
        "display_name": "Mid Cloud Model",
        "cost_per_1k_input": 0.001,
        "cost_per_1k_output": 0.002,
    },
}


def _make_result(
    model_key: str,
    prompt_tokens: int = 100,
    completion_tokens: int = 200,
    latency_ms: int = 1000,
    error: str = "",
    correlation_id: str = CORR_ID,
) -> ModelInferenceResultEntry:
    return ModelInferenceResultEntry(
        model_key=model_key,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        latency_ms=latency_ms,
        error=error,
        correlation_id=correlation_id,
        usage_source=EnumUsageSource.MEASURED,
    )


def _make_state(expected_count: int = 2) -> ModelAbCompareState:
    return ModelAbCompareState(
        correlation_id=CORR_ID,
        expected_count=expected_count,
    )


# ---------------------------------------------------------------------------
# accumulate()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_accumulate_single_result_not_yet_complete() -> None:
    """One result in a 2-model run: state is not yet completed."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=2)
    result = _make_result("local-model")

    new_state = handler.accumulate(state, result)

    assert len(new_state.results) == 1
    assert new_state.completed is False


@pytest.mark.unit
def test_accumulate_all_results_marks_completed() -> None:
    """When all expected results arrive the state is marked completed."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=2)

    state = handler.accumulate(state, _make_result("local-model"))
    state = handler.accumulate(state, _make_result("cloud-model"))

    assert len(state.results) == 2
    assert state.completed is True


@pytest.mark.unit
def test_accumulate_duplicate_model_key_ignored() -> None:
    """Duplicate result for the same model_key is discarded (idempotent)."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=2)

    state = handler.accumulate(state, _make_result("local-model"))
    state = handler.accumulate(state, _make_result("local-model"))  # duplicate

    assert len(state.results) == 1
    assert state.completed is False


@pytest.mark.unit
def test_accumulate_ignores_wrong_correlation_id() -> None:
    """Result with mismatched correlation_id is silently ignored."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=1)
    wrong = _make_result("local-model", correlation_id="other-id")

    new_state = handler.accumulate(state, wrong)

    assert len(new_state.results) == 0
    assert new_state.completed is False


@pytest.mark.unit
def test_accumulate_after_completed_is_noop() -> None:
    """Results arriving after completion are ignored — state is unchanged."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=1)
    state = handler.accumulate(state, _make_result("local-model"))
    assert state.completed is True

    state_after = handler.accumulate(state, _make_result("cloud-model"))

    assert len(state_after.results) == 1
    assert state_after.completed is True


# ---------------------------------------------------------------------------
# materialize() — returns None when incomplete
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_materialize_returns_none_when_incomplete() -> None:
    """materialize() returns None before all results are collected."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=2)
    state = handler.accumulate(state, _make_result("local-model"))

    result = handler.materialize(state, PRICING)

    assert result is None


# ---------------------------------------------------------------------------
# materialize() — cost calculation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_materialize_local_model_zero_cost() -> None:
    """Local model with 0.0 pricing produces $0.000 cost."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=1)
    state = handler.accumulate(
        state, _make_result("local-model", prompt_tokens=500, completion_tokens=1000)
    )

    completed = handler.materialize(state, PRICING)

    assert completed is not None
    local_row = next(r for r in completed.rows if r.model_key == "local-model")
    assert local_row.cost_usd == 0.0
    assert local_row.display_name == "Local Model"


@pytest.mark.unit
def test_materialize_cloud_model_cost_calculated() -> None:
    """Cloud model cost = (prompt * cost_per_1k_input + completion * cost_per_1k_output) / 1000."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=1)
    # 1000 prompt tokens at $0.003/1k = $0.003
    # 2000 completion tokens at $0.015/1k = $0.030
    # total = $0.033
    state = handler.accumulate(
        state,
        _make_result("cloud-model", prompt_tokens=1000, completion_tokens=2000),
    )

    completed = handler.materialize(state, PRICING)

    assert completed is not None
    cloud_row = next(r for r in completed.rows if r.model_key == "cloud-model")
    assert abs(cloud_row.cost_usd - 0.033) < 1e-9


@pytest.mark.unit
def test_materialize_rows_sorted_by_cost_ascending() -> None:
    """Rows are sorted cheapest first."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=3)
    state = handler.accumulate(
        state, _make_result("cloud-model", prompt_tokens=1000, completion_tokens=1000)
    )
    state = handler.accumulate(
        state, _make_result("local-model", prompt_tokens=1000, completion_tokens=1000)
    )
    state = handler.accumulate(
        state,
        _make_result("mid-cloud-model", prompt_tokens=1000, completion_tokens=1000),
    )

    completed = handler.materialize(state, PRICING)

    assert completed is not None
    costs = [r.cost_usd for r in completed.rows]
    assert costs == sorted(costs)
    assert completed.rows[0].model_key == "local-model"


@pytest.mark.unit
def test_materialize_savings_computed_correctly() -> None:
    """Savings = max_cost - min_cost."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=2)
    # local: $0.00
    state = handler.accumulate(
        state, _make_result("local-model", prompt_tokens=1000, completion_tokens=1000)
    )
    # cloud: (1000 * 0.003 + 1000 * 0.015) / 1000 = 0.003 + 0.015 = 0.018
    state = handler.accumulate(
        state, _make_result("cloud-model", prompt_tokens=1000, completion_tokens=1000)
    )

    completed = handler.materialize(state, PRICING)

    assert completed is not None
    assert abs(completed.savings_usd - 0.018) < 1e-9


@pytest.mark.unit
def test_materialize_model_count_correct() -> None:
    """model_count matches the number of rows."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=3)
    for key in ["local-model", "cloud-model", "mid-cloud-model"]:
        state = handler.accumulate(state, _make_result(key))

    completed = handler.materialize(state, PRICING)

    assert completed is not None
    assert completed.model_count == 3
    assert len(completed.rows) == 3


@pytest.mark.unit
def test_materialize_unknown_model_key_uses_fallback_pricing() -> None:
    """Model not in pricing map defaults to $0.00 cost and uses model_key as display_name."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=1)
    state = handler.accumulate(
        state, _make_result("unlisted-model", prompt_tokens=500, completion_tokens=500)
    )

    completed = handler.materialize(state, PRICING)

    assert completed is not None
    row = completed.rows[0]
    assert row.cost_usd == 0.0
    assert row.display_name == "unlisted-model"


@pytest.mark.unit
def test_materialize_error_result_included_with_zero_tokens() -> None:
    """Failed inference (error set) is included in comparison with zero token counts."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=2)
    state = handler.accumulate(state, _make_result("local-model"))
    state = handler.accumulate(
        state,
        _make_result(
            "cloud-model",
            prompt_tokens=0,
            completion_tokens=0,
            error="TimeoutException: timed out",
        ),
    )

    completed = handler.materialize(state, PRICING)

    assert completed is not None
    error_row = next(r for r in completed.rows if r.model_key == "cloud-model")
    assert error_row.error != ""
    assert error_row.cost_usd == 0.0


@pytest.mark.unit
def test_materialize_savings_never_negative() -> None:
    """savings_usd is clamped to >= 0.0 even if all models have the same cost."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=2)
    state = handler.accumulate(state, _make_result("local-model"))
    state = handler.accumulate(
        state, _make_result("local-model-2", correlation_id=CORR_ID)
    )

    # Both are $0.00 — savings should be 0.0, not negative
    pricing: PricingMap = {
        "local-model": {
            "display_name": "Local A",
            "cost_per_1k_input": 0.0,
            "cost_per_1k_output": 0.0,
        },
        "local-model-2": {
            "display_name": "Local B",
            "cost_per_1k_input": 0.0,
            "cost_per_1k_output": 0.0,
        },
    }
    completed = handler.materialize(state, pricing)

    assert completed is not None
    assert completed.savings_usd >= 0.0


@pytest.mark.unit
def test_materialize_correlation_id_preserved() -> None:
    """The completed payload carries the original correlation_id."""
    handler = HandlerAbCompareReducer()
    state = _make_state(expected_count=1)
    state = handler.accumulate(state, _make_result("local-model"))

    completed = handler.materialize(state, PRICING)

    assert completed is not None
    assert completed.correlation_id == CORR_ID
