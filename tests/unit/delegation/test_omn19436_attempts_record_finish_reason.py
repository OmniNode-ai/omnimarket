# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19436, first half: attempts record finish_reason, truncation and the preamble rule.

``finish_reason=length`` is already read at both effect boundaries. What was
missing is the RECORD: no attempt said how the provider stopped, whether the
output budget cut it off, or which reasoning-preamble rule found the seam. So
nobody could count how many runs were truncated.

This half lands everything that needs no new wire key:

* The workflow's own per-rung record, which travels as free-form
  ``escalation_history`` dicts on the terminal, carries the three facts.
* The delegate-skill attempt record fills its ALREADY-declared
  ``reasoning_preamble_rule`` (and the detail beside it) from both ladders.
  Until now that field was declared and never populated.
* The delegate-skill wire models ACCEPT the three new keys before any producer
  emits them. That is the consumer-first half the wire-compatibility gate
  requires: a released consumer must decode the new shape before the producer
  that emits it merges. The keys are declared, and emitted, by the second half.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.inference.provider_finish_reason import TRUNCATED_RESPONSE_ERROR_MESSAGE
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_escalation_attempt import (
    ModelDelegationEscalationAttempt,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.routing.model_escalation_decision_result import (
    ModelEscalationDecisionResult,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The workflow's per-rung record.
# ---------------------------------------------------------------------------


def _request(cid: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
    )


def _routing(cid: UUID, tier_name: str) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="test",
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{tier_name}"),
        endpoint_url="http://model-under-test.invalid:8000",
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale=f"Task 'test' routed via tier '{tier_name}'.",
        tier_name=tier_name,
    )


def _no_further_rung(
    workflow: DelegationWorkflowState, **_kwargs: object
) -> ModelEscalationDecisionResult:
    del workflow
    return ModelEscalationDecisionResult(
        can_escalate=False, terminal_failure_reason="fixture_no_higher_tier"
    )


def _answered(handler: HandlerDelegationWorkflow, cid: UUID, tier: str) -> None:
    handler.handle_delegation_request(_request(cid))
    handler.handle_routing_decision(_routing(cid, tier))
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="def test_foo():\n    assert True",
            model_used="qwen3-coder-30b",
            latency_ms=10,
        )
    )


def test_a_truncated_call_is_recorded_as_truncated_on_its_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: the rung the effect refused for truncation said nothing about why.

    The bus effect refuses a ``finish_reason=length`` response by raising, and
    the only channel back is the error text. The rung record now states the
    stop reason and the flag, so a count of truncated rungs is a query rather
    than a grep over prose.
    """
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_request(cid))
    handler.handle_routing_decision(_routing(cid, "claude"))
    monkeypatch.setattr(handler, "_maybe_retry_sibling_backend", lambda *_a, **_k: None)

    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="",
            model_used="qwen3-coder-30b",
            latency_ms=10,
            error_message=TRUNCATED_RESPONSE_ERROR_MESSAGE,
        )
    )

    rung = handler.workflows[cid].escalation_history[-1]
    assert rung.finish_reason is EnumProviderFinishReason.LENGTH
    assert rung.truncated is True
    # The effect refused it before any gate ran, so no preamble rule applied.
    assert rung.reasoning_preamble_rule is None
    assert rung.acceptance_reason is EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED


def test_a_call_that_failed_for_another_reason_has_no_stop_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: no response means no stop reason, which is not the same as 'absent'."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_request(cid))
    handler.handle_routing_decision(_routing(cid, "claude"))
    monkeypatch.setattr(handler, "_maybe_retry_sibling_backend", lambda *_a, **_k: None)

    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="",
            model_used="qwen3-coder-30b",
            latency_ms=10,
            error_message="503 Service Unavailable",
        )
    )

    rung = handler.workflows[cid].escalation_history[-1]
    assert rung.finish_reason is None
    assert rung.truncated is False


def test_a_gate_refused_rung_records_what_the_gate_was_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: the gate recorded the stop reason and the preamble rule; the rung dropped both."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    _answered(handler, cid, "claude")
    monkeypatch.setattr(handler, "_maybe_retry_local", lambda *_a, **_k: None)
    monkeypatch.setattr(handler, "_decide_escalation", _no_further_rung)

    handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=cid,
            passed=False,
            quality_score=0.2,
            failure_reasons=("TASK_MISMATCH: weak draft",),
            fallback_recommended=True,
            reasoning_preamble_rule="answer_marker",
            finish_reason=EnumProviderFinishReason.STOP,
        )
    )

    rung = handler.workflows[cid].escalation_history[-1]
    assert rung.acceptance_decision is not EnumDelegationAcceptanceDecision.ACCEPT
    assert rung.finish_reason is EnumProviderFinishReason.STOP
    assert rung.truncated is False
    assert rung.reasoning_preamble_rule == "answer_marker"


def test_the_accepted_rung_records_it_too() -> None:
    """RED: the rung that ANSWERED is the one a truncation audit reads first."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    _answered(handler, cid, "local")

    handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=cid,
            passed=True,
            quality_score=0.95,
            reasoning_preamble_rule="no_boundary_found",
            finish_reason=EnumProviderFinishReason.STOP,
        )
    )

    history = handler.workflows[cid].escalation_history
    accepted = [
        rung
        for rung in history
        if rung.acceptance_decision is EnumDelegationAcceptanceDecision.ACCEPT
    ]
    assert len(accepted) == 1
    assert accepted[0].finish_reason is EnumProviderFinishReason.STOP
    assert accepted[0].truncated is False
    assert accepted[0].reasoning_preamble_rule == "no_boundary_found"


def _rung(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tier_name": "local",
        "model_used": "m",
        "quality_score": 0.0,
        "latency_ms": 1,
        "fallback_recommended": True,
        "attempted_at": datetime.now(UTC).isoformat(),
        "acceptance_decision": "climb",
        "acceptance_reason": "provider_call_failed",
    }
    payload.update(overrides)
    return payload


def test_the_truncated_flag_is_derived_from_the_stop_reason() -> None:
    """A flag set beside the reason could disagree with it; derived, it cannot."""
    rung = ModelDelegationEscalationAttempt.model_validate(
        _rung(finish_reason="length")
    )
    assert rung.truncated is True


def test_a_flag_that_contradicts_the_stop_reason_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ModelDelegationEscalationAttempt.model_validate(
            _rung(finish_reason="stop", truncated=True)
        )
    message = str(excinfo.value)
    assert "truncated" in message
    assert "stop" in message


def test_a_record_from_before_these_fields_still_decodes() -> None:
    """Persisted workflow state written before this change must reload."""
    rung = ModelDelegationEscalationAttempt.model_validate(_rung())
    assert rung.finish_reason is None
    assert rung.truncated is False
    assert rung.reasoning_preamble_rule is None


# ---------------------------------------------------------------------------
# Consumer-first: the wire models accept the keys the second half will emit.
# ---------------------------------------------------------------------------


def _attempt(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tier": "local",
        "backend_id": "b",
        "model_id": "m",
        "quality_gate_passed": False,
    }
    payload.update(overrides)
    return payload


def test_the_attempt_record_accepts_the_forthcoming_keys() -> None:
    """RED: a released consumer refused both keys with extra_forbidden."""
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _attempt(finish_reason="length", truncated=True)
    )
    assert "finish_reason" not in record.model_dump()


def test_the_terminal_accepts_the_forthcoming_keys() -> None:
    """RED: the same, one level up, including the terminal's own preamble rule."""
    model = ModelDelegateSkillResponse.model_validate(
        {
            "correlation_id": str(uuid4()),
            "status": "failed",
            "task_type": "document",
            "quality_gate_passed": False,
            "quality_score": 0.0,
            "finish_reason": "length",
            "truncated": True,
            "reasoning_preamble_rule": "answer_marker",
            "attempts": [_attempt(finish_reason="length", truncated=True)],
        }
    )
    assert "truncated" not in model.model_dump()


def test_an_unknown_key_is_still_refused() -> None:
    """Control: accepting three named keys is not relaxing ``extra=forbid``."""
    with pytest.raises(ValidationError):
        ModelDelegateSkillAttemptRecord.model_validate(_attempt(finish_reasons="stop"))
    with pytest.raises(ValidationError):
        ModelDelegateSkillResponse.model_validate(
            {
                "correlation_id": str(uuid4()),
                "status": "failed",
                "task_type": "document",
                "trunctaed": True,
            }
        )


# ---------------------------------------------------------------------------
# The declared-but-never-filled preamble rule reaches the attempt record.
# ---------------------------------------------------------------------------


class _FrozenDispatchPort:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result

    async def dispatch(self, **_kwargs: object) -> dict[str, object]:
        return dict(self._result)


def _run(result: dict[str, object]) -> ModelDelegateSkillResponse:
    handler = HandlerDelegateSkill(dispatch_port=_FrozenDispatchPort(result))
    return asyncio.run(
        handler.handle(
            ModelDelegateSkillRequest(
                prompt="say ok",
                task_type="document",
                source="external-client",
                correlation_id=uuid4(),
            )
        )
    )


def test_the_local_ladder_carries_the_preamble_rule_to_the_terminal() -> None:
    """RED: the port recorded the rule on every judged rung and the handler dropped it."""
    terminal = _run(
        {
            "status": "failed",
            "error_message": "every rung refused",
            "attempts": [
                {
                    "tier": "local",
                    "backend_id": "b",
                    "model_id": "m",
                    "quality_gate_passed": False,
                    "quality_score": 0.0,
                    "acceptance_decision": "terminate",
                    "acceptance_reason": "acceptance_criteria_failed",
                    "acceptance_detail": "semantic_adequacy refused a fragment",
                    "reasoning_preamble_rule": "unpaired_closing_tag",
                    "reasoning_preamble": "<think>hmm</think>",
                    "error_message": "SHAPE_REFUSED: fragment",
                }
            ],
        }
    )

    assert isinstance(terminal, ModelDelegateSkillFailed)
    rung = terminal.attempts[0]
    assert rung.reasoning_preamble_rule == "unpaired_closing_tag"
    assert rung.reasoning_preamble == "<think>hmm</think>"
    assert rung.acceptance_detail == "semantic_adequacy refused a fragment"


def test_the_bus_ladder_carries_the_preamble_rule_to_the_terminal() -> None:
    """RED: the same fact, arriving through the workflow's escalation history."""
    terminal = _run(
        {
            "status": "failed",
            "failure_reason": "gate refused",
            "escalation_history": [
                _rung(
                    acceptance_decision="terminate",
                    acceptance_reason="acceptance_criteria_failed",
                    failure_reasons=["SHAPE_REFUSED: fragment"],
                    reasoning_preamble_rule="answer_marker",
                    finish_reason="stop",
                )
            ],
        }
    )

    rung = terminal.attempts[0]
    assert rung.reasoning_preamble_rule == "answer_marker"


def test_a_rung_no_gate_judged_keeps_no_preamble_rule() -> None:
    """Control: None means no segmentation was attempted, and stays that way."""
    terminal = _run(
        {
            "status": "failed",
            "failure_reason": "503",
            "escalation_history": [_rung(failure_reasons=["503 Service Unavailable"])],
        }
    )

    assert terminal.attempts[0].reasoning_preamble_rule is None
