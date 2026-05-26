# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Integration test: canary report → reducer → score lookup → routing policy."""

from __future__ import annotations

import pytest

from omnimarket.events.canary import ModelCanaryReport, ModelModelScore
from omnimarket.nodes.node_canary_score_reducer.handlers.handler_canary_score_reducer import (
    HandlerCanaryScoreReducer,
)
from omnimarket.nodes.node_canary_score_reducer.models.model_score_reducer_state import (
    ModelScoreReducerState,
)
from omnimarket.nodes.node_routing_policy_engine.handlers.handler_routing_policy import (
    HandlerRoutingPolicy,
)
from omnimarket.nodes.node_routing_policy_engine.handlers.handler_score_lookup import (
    build_available_models_from_scores,
)
from omnimarket.nodes.node_routing_policy_engine.models.model_routing_policy_request import (
    EnumTaskType,
    ModelRoutingPolicyRequest,
)
from omnimarket.nodes.node_routing_policy_engine.models.model_routing_policy_result import (
    EnumRoutingStatus,
    EnumSelectionMode,
)


@pytest.mark.integration
def test_canary_scores_feed_routing_decisions() -> None:
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

    reducer = HandlerCanaryScoreReducer()
    state = reducer.accumulate(ModelScoreReducerState(), report)
    result = reducer.materialize(state)

    cost_map = {"qwen3-coder-30b": 0.0001, "deepseek-r1-14b": 0.00005}
    available = build_available_models_from_scores(
        list(result.capability_score_rows), cost_map
    )

    assert len(available) == 2

    router = HandlerRoutingPolicy()
    routing_result = router.handle(
        ModelRoutingPolicyRequest(
            task_type=EnumTaskType.GENERAL,
            available_models=tuple(available),
        )
    )

    assert routing_result.status == EnumRoutingStatus.OK
    assert routing_result.selected_model_key == "qwen3-coder-30b"
    assert routing_result.selection_mode == EnumSelectionMode.EXPLOIT

    assert len(result.routing_outcome_rows) == 2
    qwen_outcome = next(
        r for r in result.routing_outcome_rows if r["model_key"] == "qwen3-coder-30b"
    )
    assert qwen_outcome["quality_score"] is not None
    assert float(qwen_outcome["quality_score"]) > 0.0  # type: ignore[arg-type]
