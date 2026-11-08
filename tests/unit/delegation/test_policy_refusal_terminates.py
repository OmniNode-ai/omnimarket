# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23216: a policy refusal on rung 1 ends the run, a 503 on rung 1 still moves on.

The task class declares ``policy_refusal`` in ``terminate_on``. The quality gate
stamps a refusal that declines on policy with ``POLICY_REFUSED_VERDICT_PREFIX``,
both delegation paths classify that verdict as the ``policy_refusal`` cause, and
the escalation decision node reads the cause from the declared lists. Fakes
only: the ladder and the provider are stubs, so no model is called and nothing is
spent.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.models.delegation.model_escalation_transitions import (
    EnumEscalationCause,
    EnumTransitionKind,
)
from omnimarket.models.delegation.wire.model_quality_gate import (
    POLICY_REFUSED_VERDICT_PREFIX,
    ModelQualityGateResult,
)
from omnimarket.models.routing.model_escalation_decision_request import (
    ModelEscalationDecisionRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
    local_quality_cause,
)
from omnimarket.nodes.node_delegation_escalation_decision_compute.handlers.handler_escalation_decision import (
    HandlerEscalationDecision,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    quality_result_cause,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _check_no_refusal,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

_LADDER = ("local", "cheap_cloud")
_BACKEND = {"local": "local-coder", "cheap_cloud": "cloud-a"}
_TIER = {value: key for key, value in _BACKEND.items()}
_MODEL = {"local": "free-model", "cheap_cloud": "paid-model"}
_GOOD_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile this "
    "approach carries in practice."
)
_POLICY_REFUSAL = "I will not help with that request; it is against my guidelines."
_CAPABILITY_REFUSAL = "I'm sorry, the request is too vague for me to answer well."
_PROMPT = "explain the tradeoff"


def _backend(tier: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=_BACKEND[tier],
        model_id=_MODEL[tier],
        endpoint_ref=f"https://{tier}-host.example:8443/v1/chat/completions",
        tier=tier,
        max_tokens=4096,
        timeout_ms=120000,
    )


@pytest.fixture(autouse=True)
def _two_rung_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A two-rung ladder, a free local rung then a cloud rung, with no size-band skip."""

    def resolve(task_type: str, *, backend_id: str | None = None) -> Any:
        return _backend(_LADDER[0] if backend_id is None else _TIER[backend_id])

    def next_tier(current: str, excluded: Any, **_: Any) -> str | None:
        later = _LADDER[_LADDER.index(current) + 1 :]
        return next((tier for tier in later if tier not in excluded), None)

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", resolve)
    monkeypatch.setattr(port_mod, "next_eligible_tier", next_tier)
    monkeypatch.setattr(port_mod, "first_eligible_tier", lambda *_a, **_k: _LADDER[0])
    monkeypatch.setattr(
        port_mod, "backend_id_for_tier", lambda t, *_a, **_k: _BACKEND[t]
    )
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda b: _TIER.get(b))
    monkeypatch.setattr(
        port_mod, "sibling_backend_available_in_tier", lambda *_a, **_k: None
    )
    monkeypatch.setattr(port_mod, "resolve_task_class_max_escalations", lambda _t: 1)
    monkeypatch.setattr(port_mod, "resolve_task_class_tier_order", lambda _t: _LADDER)
    monkeypatch.setattr(port_mod, "is_free_tier", lambda tier: tier == "local")
    monkeypatch.setattr(port_mod, "tier_max_retries", lambda _tier: 0)
    monkeypatch.setattr(port_mod, "resolve_paid_from_size_band", lambda _t: None)


class _Provider:
    """Rung 1 answers with ``first``; rung 2 answers well. Records each call."""

    def __init__(self, first: str | int) -> None:
        self._first = first
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.model_tier)
        if request.model_tier != "local":
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=True,
                content=_GOOD_ANSWER,
                tokens_in=11,
                tokens_out=22,
            )
        if isinstance(self._first, int):
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                error_message=f"provider answered HTTP {self._first}: overloaded",
                http_status=self._first,
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=self._first,
            tokens_in=11,
            tokens_out=22,
        )


def _dispatch(tmp_path: Any, provider: _Provider) -> dict[str, Any]:
    port = LocalDelegationDispatchPort(
        effect_handler=provider,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt=_PROMPT,
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )
    return result


def test_a_policy_refusal_on_rung_1_makes_no_later_attempt(tmp_path: Any) -> None:
    """AC1, refusal half: the cloud rung is never called and the run fails."""
    provider = _Provider(_POLICY_REFUSAL)

    result = _dispatch(tmp_path, provider)

    assert provider.calls == ["local"]
    assert result["status"] == "failed"
    assert result["attempts"][0]["transition"]["cause"] == "policy_refusal"
    assert result["attempts"][0]["transition"]["kind"] == "terminate"


def test_a_503_on_rung_1_still_moves_to_rung_2(tmp_path: Any) -> None:
    """AC1, 503 half: the same ladder, an unavailable rung 1, answered on rung 2."""
    provider = _Provider(503)

    result = _dispatch(tmp_path, provider)

    assert provider.calls == ["local", "cheap_cloud"]
    assert result["status"] == "completed"
    assert result["attempts"][-1]["tier"] == "cheap_cloud"


def test_control_a_refusal_that_is_not_a_policy_refusal_still_climbs(
    tmp_path: Any,
) -> None:
    """Failing control: a capability-gap apology is a plain refusal and a cloud rung may cure it."""
    provider = _Provider(_CAPABILITY_REFUSAL)

    result = _dispatch(tmp_path, provider)

    assert provider.calls == ["local", "cheap_cloud"]
    assert result["status"] == "completed"


@pytest.mark.parametrize(
    ("content", "policy"),
    [
        (_POLICY_REFUSAL, True),
        ("I cannot assist with that.", True),
        ("I won't do that.", True),
        (_CAPABILITY_REFUSAL, False),
        ("I cannot see the file you mention.", False),
    ],
)
def test_the_gate_marks_only_a_policy_refusal(content: str, policy: bool) -> None:
    """The gate's refusal classification stamps the policy prefix on policy phrases alone."""
    reason = _check_no_refusal(content)

    assert reason is not None
    assert reason.startswith("REFUSAL")
    assert reason.startswith(POLICY_REFUSED_VERDICT_PREFIX) is policy
    result = ModelQualityGateResult(
        correlation_id=uuid4(),
        passed=False,
        quality_score=0.0,
        fail_category="fail_heuristic",
        failure_reasons=("WEAK_OUTPUT: response length 3 below minimum 80", reason),
        fallback_recommended=True,
    )
    assert result.policy_refused is policy


def test_both_paths_classify_the_policy_verdict_as_the_terminal_cause() -> None:
    """The bus and the local port read one verdict as one declared cause."""
    for classify in (quality_result_cause, local_quality_cause):
        assert (
            classify(
                no_rung_can_satisfy=False,
                fail_category="fail_heuristic",
                policy_refused=True,
            )
            is EnumEscalationCause.POLICY_REFUSAL
        )
        assert (
            classify(no_rung_can_satisfy=False, fail_category="fail_heuristic")
            is EnumEscalationCause.QUALITY_GATE_REFUSED
        )


def test_every_task_class_declares_policy_refusal_terminal() -> None:
    """The contract, not the node, owns the rule: every class lists it in terminate_on."""
    authority = load_task_class_authority()
    for name, entry in authority.task_classes.items():
        assert entry.transitions is not None, name
        assert (
            entry.transitions.kind_of(EnumEscalationCause.POLICY_REFUSAL)
            is EnumTransitionKind.TERMINATE
        ), name
        assert (
            entry.transitions.kind_of(EnumEscalationCause.TRANSIENT_PROVIDER_ERROR)
            is EnumTransitionKind.RETRY
        ), name


def test_the_decision_node_terminates_on_the_cause_and_moves_on_a_spent_503() -> None:
    """The bus path's decision: policy_refusal ends the run, a spent transient moves."""
    authority = load_task_class_authority()
    transitions = authority.task_classes["research"].transitions
    assert transitions is not None
    decide = HandlerEscalationDecision().handle

    refused = decide(
        ModelEscalationDecisionRequest(
            cause=EnumEscalationCause.POLICY_REFUSAL,
            transitions=transitions,
            escalation_count=0,
            max_escalation_attempts=1,
            current_tier_name="local",
            next_tier_name="cheap_cloud",
            error_retryable=True,
            non_retryable_reason="policy_refused",
        )
    )
    assert refused.can_escalate is False
    assert refused.terminal_failure_reason == "policy_refused"

    unavailable = decide(
        ModelEscalationDecisionRequest(
            cause=EnumEscalationCause.RETRY_BUDGET_SPENT,
            transitions=transitions,
            escalation_count=0,
            max_escalation_attempts=1,
            current_tier_name="local",
            next_tier_name="cheap_cloud",
            error_retryable=True,
            non_retryable_reason="policy_refused",
        )
    )
    assert unavailable.can_escalate is True
    assert unavailable.next_tier_name == "cheap_cloud"
