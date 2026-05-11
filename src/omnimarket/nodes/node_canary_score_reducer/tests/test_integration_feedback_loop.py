# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""End-to-end integration test: canary → scores → routing feedback loop.

Proves the full chain works in-memory, without any database:
  1. ModelCanaryReport (2 models, clear winner) →
  2. HandlerCanaryScoreReducer.accumulate() →
  3. HandlerCanaryScoreReducer.materialize() → ModelMaterializeResult →
  4. build_available_models_from_scores() → list[ModelAvailableModel] →
  5. HandlerRoutingPolicy.handle() → selects champion (qwen3-coder-30b) →
  6. routing_outcome_rows contain quality_score > 0.0

[OMN-10848]
"""

from __future__ import annotations

import pytest

from omnimarket.events.canary import ModelCanaryReport, ModelModelScore
from omnimarket.nodes.node_canary_score_reducer.handlers.handler_canary_score_reducer import (
    HandlerCanaryScoreReducer,
)
from omnimarket.nodes.node_canary_score_reducer.models.model_score_reducer_state import (
    ModelScoreReducerState,
)
from omnimarket.nodes.node_routing_policy_engine import (
    EnumTaskType,
    HandlerRoutingPolicy,
    ModelRoutingPolicyRequest,
    build_available_models_from_scores,
)


@pytest.mark.integration
def test_canary_scores_feed_routing_decisions() -> None:
    """End-to-end: canary report → reducer → score lookup → routing selects champion."""
    # 1. Simulate canary report with clear winner (qwen3-coder-30b: 0.94 recall vs deepseek: 0.70)
    report = ModelCanaryReport(
        run_id="canary-integration-001",
        manifest_path="/tmp/manifest.yaml",
        entries_total=10,
        entries_completed=10,
        entries_failed=0,
        model_scores=[
            ModelModelScore(
                model_key="qwen3-coder-30b",
                entries_evaluated=10,
                entries_failed=0,
                avg_recall=0.94,
                avg_precision=0.91,
                avg_fidelity=0.88,
                avg_format_compliance=0.95,
                total_latency_ms=45000,
                estimated_cost_usd=0.12,
            ),
            ModelModelScore(
                model_key="deepseek-r1-14b",
                entries_evaluated=10,
                entries_failed=0,
                avg_recall=0.70,
                avg_precision=0.65,
                avg_fidelity=0.60,
                avg_format_compliance=0.80,
                total_latency_ms=32000,
                estimated_cost_usd=0.08,
            ),
        ],
        evidence_dir="/tmp/evidence",
        scorecard_path="/tmp/scorecard.md",
        dry_run=False,
        success=True,
    )

    # 2. Reducer accumulates and materializes
    reducer = HandlerCanaryScoreReducer()
    state = reducer.accumulate(ModelScoreReducerState(), report)
    result = reducer.materialize(state)

    # Verify intermediate state
    assert len(result.capability_score_rows) == 2

    # 3. Score lookup bridges capability_score_rows to routing inputs
    cost_map = {"qwen3-coder-30b": 0.0001, "deepseek-r1-14b": 0.00005}
    available = build_available_models_from_scores(
        result.capability_score_rows,
        cost_map,
    )
    assert len(available) == 2

    # 4. Routing selects the champion — no exploration seed → always exploit
    router = HandlerRoutingPolicy()
    routing_result = router.handle(
        ModelRoutingPolicyRequest(
            task_type=EnumTaskType.GENERAL,
            available_models=tuple(available),
        ),
    )

    # 5. Verify: Qwen3-Coder wins (highest composite score)
    assert routing_result.selected_model_key == "qwen3-coder-30b"
    # EnumSelectionMode is a StrEnum; value is "exploit" (lowercase)
    assert routing_result.selection_mode is not None
    assert str(routing_result.selection_mode) == "exploit"

    # 6. Verify: routing_outcome_rows are populated with quality_score > 0.0
    assert len(result.routing_outcome_rows) == 2

    qwen_outcome = next(
        r for r in result.routing_outcome_rows if r["model_key"] == "qwen3-coder-30b"
    )
    assert qwen_outcome["quality_score"] is not None
    assert float(str(qwen_outcome["quality_score"])) > 0.0
    assert qwen_outcome["selected"] is True

    ds_outcome = next(
        r for r in result.routing_outcome_rows if r["model_key"] == "deepseek-r1-14b"
    )
    assert ds_outcome["quality_score"] is not None
    assert float(str(ds_outcome["quality_score"])) > 0.0
    assert ds_outcome["selected"] is False
