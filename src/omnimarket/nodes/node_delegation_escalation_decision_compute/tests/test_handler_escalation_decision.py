# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerEscalationDecision — pure deterministic escalation/tier decision.

OMN-13476: covers the contract's escalation cases — tier-ladder progression,
max-attempts -> escalate-no-more, refusal/non-retryable -> terminate,
quality-gate-fail -> escalate, success -> no-escalate (not this handler's path,
asserted via the orchestrator delegation), and ladder-exhaustion -> terminate.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_delegation_escalation_decision_compute.handlers.handler_escalation_decision import (
    HandlerEscalationDecision,
)
from omnimarket.routing.model_escalation_decision_request import (
    ModelEscalationDecisionRequest,
)
from omnimarket.routing.model_escalation_decision_result import (
    ModelEscalationDecisionResult,
)


def _req(**overrides: object) -> ModelEscalationDecisionRequest:
    base: dict[str, object] = {
        "escalation_count": 0,
        "max_escalation_attempts": 2,
        "current_tier_name": "local",
        "error_retryable": True,
        "next_tier_name": "cheap_cloud",
        "non_retryable_reason": "non_retryable_inference_response",
        "no_higher_tier_reason": None,
    }
    base.update(overrides)
    return ModelEscalationDecisionRequest.model_validate(base)


@pytest.mark.unit
class TestTierLadderProgression:
    def test_quality_gate_fail_escalates_to_next_tier(self) -> None:
        # quality-gate-fail path: error_retryable=True, a routable next tier.
        result = HandlerEscalationDecision().handle(
            _req(current_tier_name="local", next_tier_name="cheap_cloud")
        )
        assert result.can_escalate is True
        assert result.next_tier_name == "cheap_cloud"
        assert result.terminal_failure_reason is None

    def test_progression_advances_through_ladder(self) -> None:
        # second rung: local -> cheap_cloud already done, now cheap_cloud -> claude.
        result = HandlerEscalationDecision().handle(
            _req(
                escalation_count=1,
                current_tier_name="cheap_cloud",
                next_tier_name="claude",
            )
        )
        assert result.can_escalate is True
        assert result.next_tier_name == "claude"


@pytest.mark.unit
class TestMaxAttemptsTerminates:
    def test_at_max_attempts_does_not_escalate(self) -> None:
        result = HandlerEscalationDecision().handle(
            _req(escalation_count=2, max_escalation_attempts=2)
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "max_escalation_attempts_reached"
        assert result.next_tier_name is None

    def test_over_max_attempts_does_not_escalate(self) -> None:
        result = HandlerEscalationDecision().handle(
            _req(escalation_count=5, max_escalation_attempts=2)
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "max_escalation_attempts_reached"

    def test_budget_check_precedes_tier_resolution(self) -> None:
        # Even with a routable next tier, an exhausted budget terminates.
        result = HandlerEscalationDecision().handle(
            _req(
                escalation_count=2,
                max_escalation_attempts=2,
                next_tier_name="claude",
            )
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "max_escalation_attempts_reached"


@pytest.mark.unit
class TestNonRetryableTerminates:
    def test_non_retryable_error_terminates_with_supplied_reason(self) -> None:
        result = HandlerEscalationDecision().handle(
            _req(
                error_retryable=False,
                non_retryable_reason="non_retryable_inference_response",
                next_tier_name="claude",
            )
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "non_retryable_inference_response"
        assert result.next_tier_name is None

    def test_non_retryable_precedes_budget_and_tier(self) -> None:
        # Non-retryable wins even with budget and a routable tier available.
        result = HandlerEscalationDecision().handle(
            _req(
                error_retryable=False,
                escalation_count=0,
                max_escalation_attempts=99,
                next_tier_name="claude",
                non_retryable_reason="empty_choices_array",
            )
        )
        assert result.terminal_failure_reason == "empty_choices_array"


@pytest.mark.unit
class TestLadderExhaustionTerminates:
    def test_no_higher_tier_terminates_with_precise_reason(self) -> None:
        result = HandlerEscalationDecision().handle(
            _req(
                next_tier_name=None,
                no_higher_tier_reason="no_higher_tier_available: claude excluded",
            )
        )
        assert result.can_escalate is False
        assert (
            result.terminal_failure_reason
            == "no_higher_tier_available: claude excluded"
        )

    def test_no_higher_tier_without_reason_raises(self) -> None:
        with pytest.raises(ValueError, match="no_higher_tier_reason"):
            HandlerEscalationDecision().handle(
                _req(next_tier_name=None, no_higher_tier_reason=None)
            )


@pytest.mark.unit
class TestCurrentTierUnknownTerminates:
    def test_current_tier_none_terminates(self) -> None:
        result = HandlerEscalationDecision().handle(
            _req(current_tier_name=None, next_tier_name="claude")
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "current_tier_unknown"


@pytest.mark.unit
class TestDeterminismAndPurity:
    def test_same_input_same_output(self) -> None:
        handler = HandlerEscalationDecision()
        req = _req()
        first = handler.handle(req)
        second = handler.handle(req)
        assert first == second

    def test_result_is_frozen(self) -> None:
        result = HandlerEscalationDecision().handle(_req())
        with pytest.raises(ValidationError):
            result.can_escalate = False  # type: ignore[misc]


@pytest.mark.unit
class TestResultModelInvariant:
    def test_escalate_requires_next_tier(self) -> None:
        with pytest.raises(ValidationError):
            ModelEscalationDecisionResult(
                can_escalate=True,
                next_tier_name=None,
                terminal_failure_reason=None,
            )

    def test_terminate_requires_reason(self) -> None:
        with pytest.raises(ValidationError):
            ModelEscalationDecisionResult(
                can_escalate=False,
                next_tier_name=None,
                terminal_failure_reason=None,
            )

    def test_escalate_rejects_terminal_reason(self) -> None:
        with pytest.raises(ValidationError):
            ModelEscalationDecisionResult(
                can_escalate=True,
                next_tier_name="claude",
                terminal_failure_reason="oops",
            )
