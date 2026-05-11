# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerCanaryScoreReducer.

[OMN-10845] [OMN-10847]
"""

from __future__ import annotations

import pytest

from omnimarket.events.canary import ModelCanaryReport, ModelModelScore
from omnimarket.nodes.node_canary_score_reducer.handlers.handler_canary_score_reducer import (
    TASK_TYPE,
    WEIGHT_FIDELITY,
    WEIGHT_FORMAT,
    WEIGHT_PRECISION,
    WEIGHT_RECALL,
    HandlerCanaryScoreReducer,
)
from omnimarket.nodes.node_canary_score_reducer.models.model_score_reducer_state import (
    ModelScoreReducerState,
)


def _make_report(
    *,
    run_id: str = "20260511-120000-abc123",
    success: bool = True,
    model_scores: list[ModelModelScore] | None = None,
) -> ModelCanaryReport:
    return ModelCanaryReport(
        run_id=run_id,
        manifest_path="/fake/manifest.yaml",
        model_scores=model_scores or [],
        evidence_dir="/fake/evidence",
        scorecard_path="/fake/scorecard.md",
        success=success,
    )


@pytest.mark.unit
def test_accumulate_updates_state_from_canary_report() -> None:
    """accumulate() with a successful report produces state with 2 scored entries."""
    handler = HandlerCanaryScoreReducer()
    state = ModelScoreReducerState()

    report = _make_report(
        model_scores=[
            ModelModelScore(
                model_key="qwen3-coder-30b",
                entries_evaluated=10,
                avg_recall=0.94,
                avg_precision=0.91,
                avg_fidelity=0.88,
                avg_format_compliance=0.95,
                total_latency_ms=5000,
                estimated_cost_usd=0.05,
            ),
            ModelModelScore(
                model_key="deepseek-r1-14b",
                entries_evaluated=10,
                avg_recall=0.87,
                avg_precision=0.83,
                avg_fidelity=0.80,
                avg_format_compliance=0.90,
                total_latency_ms=4000,
                estimated_cost_usd=0.03,
            ),
        ]
    )

    new_state = handler.accumulate(state, report)

    assert len(new_state.scores) == 2

    qwen_key = f"qwen3-coder-30b::{TASK_TYPE}"
    deep_key = f"deepseek-r1-14b::{TASK_TYPE}"
    assert qwen_key in new_state.scores
    assert deep_key in new_state.scores

    qwen_row = new_state.scores[qwen_key]
    deep_row = new_state.scores[deep_key]

    assert qwen_row.avg_recall == 0.94
    assert qwen_row.canary_run_id == "20260511-120000-abc123"

    # qwen composite must exceed deepseek composite
    assert qwen_row.composite_score is not None
    assert deep_row.composite_score is not None
    assert qwen_row.composite_score > deep_row.composite_score


@pytest.mark.unit
def test_composite_score_weights_recall_and_precision_highest() -> None:
    """High recall+precision with low fidelity+format beats low recall with high everything."""
    handler = HandlerCanaryScoreReducer()

    # Model A: high recall+precision, low fidelity+format
    composite_a = handler.compute_composite(
        recall=1.0,
        precision=1.0,
        fidelity=0.0,
        format_compliance=0.0,
    )
    # Weight of recall+precision = 0.35 + 0.35 = 0.70
    # composite_a = (1.0*0.35 + 1.0*0.35 + 0.0*0.20 + 0.0*0.10) / 1.0 = 0.70

    # Model B: low recall, high everything else
    composite_b = handler.compute_composite(
        recall=0.0,
        precision=0.5,
        fidelity=1.0,
        format_compliance=1.0,
    )
    # composite_b = (0.0*0.35 + 0.5*0.35 + 1.0*0.20 + 1.0*0.10) / 1.0 = 0.175 + 0.20 + 0.10 = 0.475

    assert composite_a is not None
    assert composite_b is not None
    assert composite_a > composite_b

    # Verify the weight constants sum correctly
    assert abs((WEIGHT_RECALL + WEIGHT_PRECISION) - 0.70) < 1e-9
    assert (
        abs((WEIGHT_RECALL + WEIGHT_PRECISION + WEIGHT_FIDELITY + WEIGHT_FORMAT) - 1.0)
        < 1e-9
    )


@pytest.mark.unit
def test_materialize_produces_capability_score_rows() -> None:
    """materialize() produces rows with expected keys matching capability_scores schema."""
    handler = HandlerCanaryScoreReducer()
    state = ModelScoreReducerState()

    report = _make_report(
        run_id="20260511-130000-xyz789",
        model_scores=[
            ModelModelScore(
                model_key="qwen3-coder-30b",
                entries_evaluated=5,
                avg_recall=0.90,
                avg_precision=0.88,
                avg_fidelity=0.85,
                avg_format_compliance=0.92,
                total_latency_ms=2500,
                estimated_cost_usd=0.02,
            ),
        ],
    )
    new_state = handler.accumulate(state, report)
    result = handler.materialize(new_state)

    assert len(result.capability_score_rows) == 1
    row = result.capability_score_rows[0]

    # Required capability_scores table columns
    assert row["model_key"] == "qwen3-coder-30b"
    assert row["task_type"] == TASK_TYPE
    assert row["success_rate"] is not None  # composite_score
    assert row["avg_latency_ms"] == 2500.0 / 5  # 500.0
    assert row["total_cost"] == 0.02
    assert row["total_count"] == 5
    assert row["success_count"] == 5
    assert row["failure_count"] == 0

    # Verify composite_score is plumbed through as success_rate
    score_row = new_state.scores[f"qwen3-coder-30b::{TASK_TYPE}"]
    assert row["success_rate"] == score_row.composite_score


@pytest.mark.unit
def test_accumulate_skips_failed_report() -> None:
    """accumulate() with success=False returns the state unchanged."""
    handler = HandlerCanaryScoreReducer()
    state = ModelScoreReducerState()

    failed_report = _make_report(
        success=False,
        model_scores=[
            ModelModelScore(
                model_key="qwen3-coder-30b",
                entries_evaluated=3,
                avg_recall=0.80,
                avg_precision=0.75,
            ),
        ],
    )

    new_state = handler.accumulate(state, failed_report)

    assert new_state is state
    assert len(new_state.scores) == 0


@pytest.mark.unit
def test_materialize_preserves_entries_failed() -> None:
    """materialize() propagates entries_failed into failure_count and adjusts success_count."""
    handler = HandlerCanaryScoreReducer()
    state = ModelScoreReducerState()

    report = _make_report(
        model_scores=[
            ModelModelScore(
                model_key="qwen3-coder-30b",
                entries_evaluated=10,
                entries_failed=3,
                avg_recall=0.90,
                avg_precision=0.88,
            ),
        ],
    )
    new_state = handler.accumulate(state, report)
    result = handler.materialize(new_state)

    assert len(result.capability_score_rows) == 1
    row = result.capability_score_rows[0]
    assert row["total_count"] == 10
    assert row["failure_count"] == 3
    assert row["success_count"] == 7


@pytest.mark.unit
def test_materialize_produces_routing_outcome_rows() -> None:
    """materialize() produces routing_outcome_rows with quality_score == composite_score."""
    handler = HandlerCanaryScoreReducer()
    from omnimarket.nodes.node_canary_score_reducer.models.model_score_reducer_state import (
        ModelCapabilityScoreRow,
    )

    state = ModelScoreReducerState(
        scores={
            "qwen3-coder-30b::adr_extraction": ModelCapabilityScoreRow(
                model_key="qwen3-coder-30b",
                task_type="adr_extraction",
                avg_recall=0.94,
                avg_precision=0.91,
                avg_fidelity=0.88,
                avg_format_compliance=0.95,
                composite_score=0.92,
                entries_evaluated=10,
                estimated_cost_usd=0.12,
                total_latency_ms=45000,
                canary_run_id="canary-001",
            ),
        },
    )

    result = handler.materialize(state)

    assert len(result.routing_outcome_rows) == 1
    row = result.routing_outcome_rows[0]
    assert row["quality_score"] == pytest.approx(0.92, abs=0.01)
    assert row["model_key"] == "qwen3-coder-30b"
    assert row["task_type"] == "adr_extraction"
    assert row["correlation_id"] == "canary-001"
    assert row["selected"] is True
