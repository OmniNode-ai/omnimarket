# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A run the handler budget cancelled names ``timeout`` as its cause.

Operator RULING 2026-10-10T00:27:40Z: a budget-cancelled run reports cause
``timeout``, and any earlier quality-gate refusals stay listed in its attempts.
OMN-19004 reads a ladder with a gate refusal and no acceptance as gate-decided;
that reading still holds for every run the budget did not cancel, because those
runs ended on their own ladder. A cancelled run did not: the budget stopped it
while a rung was still in flight, so the gate never had the last word.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import EnumDelegationTerminalFailureCause

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    delegate_skill_terminal_from_response,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.dispatch_progress import (
    budget_cancelled_result,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _response_from_result,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegation_dispatch_progress import (
    ModelDelegationDispatchProgress,
)

pytestmark = pytest.mark.unit

_RUNG: dict[str, object] = {
    "tier": "local",
    "backend_id": "local-coder",
    "model_id": "served-model",
    "cost_usd": 0.0,
}
_GATE_REFUSED: dict[str, object] = {
    **_RUNG,
    "quality_gate_passed": False,
    "quality_score": 0.4,
    "error_message": "",
    "acceptance_decision": EnumDelegationAcceptanceDecision.CLIMB.value,
    "acceptance_reason": EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR.value,
}
_THROTTLED: dict[str, object] = {
    **_RUNG,
    "tier": "cheap_frontier",
    "backend_id": "openrouter-nemotron-ultra",
    "quality_gate_passed": False,
    "quality_score": None,
    "failure_class": "rate_limited",
    "http_status": 429,
    "error_message": "HTTP 429 Too Many Requests",
    "acceptance_decision": EnumDelegationAcceptanceDecision.CLIMB.value,
    "acceptance_reason": EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED.value,
}
_IN_FLIGHT: dict[str, object] = {
    **_RUNG,
    "tier": "cheap_cloud",
    "backend_id": "cloud-gemini-flash",
}
_CANCEL = (
    "delegation exceeded the handler execution budget of 5s and was cancelled "
    "at stage=inference"
)


def _request() -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="explain what a calendar app needs",
        task_type="research",
        source="claude-code",
        correlation_id=uuid4(),
    )


def _terminal(
    result: dict[str, object],
) -> ModelDelegateSkillCompleted | ModelDelegateSkillFailed:
    return delegate_skill_terminal_from_response(
        _response_from_result(
            _request(),
            result,
            tenant_id=None,
            queue_wait_ms=None,
            execution_duration_ms=5000,
            budget_evidence=None,
        )
    )


def _cancelled(
    earlier: list[dict[str, object]],
) -> ModelDelegateSkillCompleted | ModelDelegateSkillFailed:
    progress = ModelDelegationDispatchProgress(
        attempts=[dict(rung) for rung in earlier],
        in_flight_attempt=dict(_IN_FLIGHT),
    )
    return _terminal(
        budget_cancelled_result(
            progress, cancel_message=_CANCEL, cancelled_stage="inference"
        )
    )


@pytest.mark.parametrize(
    "earlier",
    [
        pytest.param([_GATE_REFUSED, _THROTTLED], id="gate-refusal-then-429"),
        pytest.param([_THROTTLED, _GATE_REFUSED], id="429-then-gate-refusal"),
    ],
)
def test_a_budget_cancelled_run_names_timeout_and_keeps_its_gate_refusal(
    earlier: list[dict[str, object]],
) -> None:
    terminal = _cancelled(earlier)

    assert terminal.status == "timeout"
    assert terminal.terminal_failure_cause is EnumDelegationTerminalFailureCause.TIMEOUT
    attempts = terminal.attempts
    reasons = [attempt.acceptance_reason for attempt in attempts]
    assert EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR in reasons
    assert attempts[-1].failure_class == "timeout"
    assert attempts[-1].acceptance_decision is None


def test_a_run_that_ended_on_its_own_ladder_is_still_decided_by_the_gate() -> None:
    # Positive control for OMN-19004: no budget cancel, a gate refusal, then a
    # real 429 on the last rung. The gate decided the run, as before.
    terminal = _terminal(
        {
            "status": "failed",
            "content": "",
            "error_message": "HTTP 429 Too Many Requests",
            "terminal_failure_cause": "provider_quota_exhausted",
            "attempts": [dict(_GATE_REFUSED), dict(_THROTTLED)],
        }
    )

    assert terminal.status == "failed"
    assert (
        terminal.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED
    )
