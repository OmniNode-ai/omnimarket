# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public delegate-skill preservation of canonical terminal truth (OMN-15539)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import (
    EnumDelegationTerminalFailureCause,
    EnumQualityScoreComparison,
)

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)

_CONTRACT_PATH = (
    Path(__file__).parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)


def _request() -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="Review the change.",
        task_type="review",
        source="codex",
    )


@pytest.mark.unit
def test_response_structured_truth_round_trips_and_legacy_defaults_stay_additive() -> (
    None
):
    legacy = ModelDelegateSkillResponse(
        status="completed",
        correlation_id=uuid4(),
        task_type="test",
    )
    assert legacy.required_quality_bar is None
    assert legacy.score_vs_required_bar is None
    assert legacy.failed_acceptance_criteria == ()
    assert legacy.terminal_failure_cause is None
    assert legacy.attempts_count == 1

    response = ModelDelegateSkillResponse(
        status="failed",
        correlation_id=uuid4(),
        task_type="review",
        quality_score=0.9,
        required_quality_bar=0.85,
        score_vs_required_bar=EnumQualityScoreComparison.AT_OR_ABOVE_BAR,
        failed_acceptance_criteria=("missing independent evidence",),
        terminal_failure_cause=(
            EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
        ),
        attempts_count=3,
    )

    dumped = response.model_dump_json()
    restored = ModelDelegateSkillResponse.model_validate_json(dumped)
    assert restored == response
    assert '"score_vs_required_bar":"at_or_above_bar"' in dumped
    assert '"terminal_failure_cause":"provider_quota_exhausted"' in dumped


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param(
            {"required_quality_bar": 0.85},
            "must be provided together",
            id="partial-bar-evidence",
        ),
        pytest.param(
            {
                "quality_score": 0.9,
                "required_quality_bar": 0.85,
                "score_vs_required_bar": "below_bar",
            },
            "must match quality_score",
            id="comparison-contradicts-numbers",
        ),
        pytest.param(
            {
                "quality_gate_passed": True,
                "failed_acceptance_criteria": ["missing evidence"],
            },
            "cannot carry failed_acceptance_criteria",
            id="passed-with-failed-criteria",
        ),
        pytest.param(
            {
                "quality_gate_passed": True,
                "terminal_failure_cause": "provider_quota_exhausted",
            },
            "cannot carry terminal_failure_cause",
            id="passed-with-terminal-cause",
        ),
    ],
)
def test_response_rejects_contradictory_structured_truth(
    overrides: dict[str, object],
    message: str,
) -> None:
    payload: dict[str, object] = {
        "status": "completed",
        "correlation_id": uuid4(),
        "task_type": "review",
    }
    payload.update(overrides)

    with pytest.raises(ValueError, match=message):
        ModelDelegateSkillResponse.model_validate(payload)


@pytest.mark.unit
async def test_handler_maps_structured_truth_and_history_as_best_effort() -> None:
    routing_decision_id = uuid4()
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "failed",
        "content": "A scored answer that still missed a required criterion.",
        "quality_passed": False,
        "quality_score": 0.9,
        "required_quality_bar": 0.85,
        "score_vs_required_bar": "at_or_above_bar",
        "failed_acceptance_criteria": ["missing independent evidence"],
        "terminal_failure_cause": "provider_quota_exhausted",
        "attempts_count": 3,
        "compliance_attempts": 3,
        "failure_reason": "provider quota exhausted after repair attempts",
        "escalation_history": [
            {
                "tier_name": "cheap_cloud",
                "model_used": "gemini-flash",
                "quality_score": 0.72,
                "failure_reasons": ["missing independent evidence"],
                "cost_usd": 0.001,
                "routing_decision_id": str(routing_decision_id),
                "attempted_at": datetime.now(UTC).isoformat(),
            }
        ],
    }

    response = await HandlerDelegateSkill(dispatch_port=port).handle(_request())

    assert response.required_quality_bar == 0.85
    assert response.score_vs_required_bar is EnumQualityScoreComparison.AT_OR_ABOVE_BAR
    assert response.failed_acceptance_criteria == ("missing independent evidence",)
    assert response.terminal_failure_cause is (
        EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
    )
    assert response.attempts_count == 3
    assert len(response.attempts) == 1
    assert response.attempts[0].tier == "cheap_cloud"
    assert response.attempts[0].backend_id == str(routing_decision_id)
    assert response.attempts[0].model_id == "gemini-flash"
    assert response.attempts[0].quality_gate_passed is False
    assert response.attempts[0].error_message == "missing independent evidence"


@pytest.mark.unit
async def test_handler_prefers_richer_attempts_over_escalation_history() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "completed",
        "content": "accepted",
        "attempts_count": 2,
        "attempts": [
            {
                "tier": "cheap_cloud",
                "backend_id": "cloud-gemini",
                "model_id": "gemini-flash",
                "quality_gate_passed": True,
                "quality_score": 0.93,
                "cost_usd": 0.001,
            }
        ],
        "escalation_history": [
            {
                "tier_name": "local",
                "model_used": "local-model",
                "quality_score": 0.4,
                "failure_reasons": ["rejected"],
            }
        ],
    }

    response = await HandlerDelegateSkill(dispatch_port=port).handle(_request())

    assert response.attempts_count == 2
    assert [attempt.tier for attempt in response.attempts] == ["cheap_cloud"]
    assert response.attempts[0].quality_gate_passed is True


@pytest.mark.unit
def test_projection_accepts_and_preserves_additive_structured_truth() -> None:
    payload = {
        "status": "failed",
        "correlation_id": str(uuid4()),
        "task_type": "review",
        "quality_score": 0.8,
        "required_quality_bar": 0.85,
        "score_vs_required_bar": "below_bar",
        "failed_acceptance_criteria": ["missing evidence"],
        "terminal_failure_cause": "provider_quota_exhausted",
        "attempts_count": 4,
    }

    projection = ModelDelegateSkillTerminalProjection.from_payload(payload)

    assert projection.required_quality_bar == 0.85
    assert projection.score_vs_required_bar is EnumQualityScoreComparison.BELOW_BAR
    assert projection.failed_acceptance_criteria == ("missing evidence",)
    assert projection.terminal_failure_cause is (
        EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
    )
    assert projection.attempts_count == 4


@pytest.mark.unit
def test_node_contract_declares_additive_terminal_truth_outputs() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["contract_version"] == {"major": 1, "minor": 1, "patch": 0}
    assert contract["node_version"] == {"major": 1, "minor": 1, "patch": 0}
    assert {
        "required_quality_bar",
        "score_vs_required_bar",
        "failed_acceptance_criteria",
        "terminal_failure_cause",
        "attempts_count",
    }.issubset(contract["outputs"])
