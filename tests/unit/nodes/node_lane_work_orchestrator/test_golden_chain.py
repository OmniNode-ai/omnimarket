# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the help step: stuck worker, first tier fails, second answers, the workflow fixes (OMN-26722)."""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_lane_work_orchestrator.handlers.handler_lane_work_help import (
    HandlerLaneWorkHelp,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_help_io import (
    ModelLaneWorkHelpAnswerInput,
)

from .conftest import build_request, driver_in_help

pytestmark = pytest.mark.unit


def test_golden_chain_stuck_sonnet_worker_climbs_then_is_answered() -> None:
    handler = HandlerLaneWorkHelp()
    driver = driver_in_help("glm-large")

    first = handler.build(build_request(driver))
    assert first.delegate_request is not None
    assert first.delegate_request.backend_id == "harness-codex-cheapest"
    tier = driver.state.help_tiers[driver.state.help_tier_index]
    failed = handler.validate_answer(
        ModelLaneWorkHelpAnswerInput(
            state=driver.state,
            correlation_id=str(first.delegate_request.correlation_id),
            failed=True,
            executed_backend_id=tier.backend_id,
            executed_model=tier.model,
        )
    )
    assert failed.fold_event is not None
    driver.send("help_failed", action_id=failed.fold_event.action_id)

    second = handler.build(build_request(driver))
    assert second.delegate_request is not None
    assert second.delegate_request.backend_id == "harness-claude-code-small"
    tier = driver.state.help_tiers[driver.state.help_tier_index]
    answered = handler.validate_answer(
        ModelLaneWorkHelpAnswerInput(
            state=driver.state,
            correlation_id=str(second.delegate_request.correlation_id),
            answer_text=json.dumps({"kind": "help_cause_found", "cause": "stale base"}),
            executed_backend_id=tier.backend_id,
            executed_model=tier.model,
        )
    )
    assert answered.fold_event is not None
    assert (answered.receipt.asking_tier, answered.receipt.answering_tier) == (
        "codex-cheapest",
        "claude-small",
    )
    driver.send(
        answered.fold_event.kind,
        action_id=answered.fold_event.action_id,
        evidence=answered.fold_event.evidence,
    )
    assert driver.name == "fixing"
