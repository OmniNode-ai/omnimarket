# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retry-local (best-of-N) on the bus orchestrator FSM [OMN-14234].

The local coder is non-deterministic: a single trivial refactor scored
0.8 / 0.64 / 1.0 across three runs at the 0.85 bar, so ~2/3 of first drafts
escalated to a PAID tier despite local inference being $0. OMN-14234 makes
``handle_gate_result`` retry the SAME free tier up to its contract-declared
``max_retries`` budget BEFORE escalating off it — the first draft that clears the
gate lands local at $0, and only after the budget is exhausted does the workflow
escalate to a paid tier.

These tests drive the FSM directly and control the free-tier gate + budget via
monkeypatch so the retry-local logic is proven in isolation from live routing
config:

  * ``is_free_tier`` — ``local`` is free, everything else paid.
  * ``tier_max_retries`` — ``local`` tolerates 2 retries (1 initial + 2 = 3 $0
    drafts); paid tiers tolerate 0.
  * ``next_eligible_tier`` — a deterministic ladder ``local -> cheap_cloud ->
    claude`` so the escalation leg after budget exhaustion resolves a next tier.

The escalation MECHANICS themselves are covered in
``test_delegation_tier_escalation.py`` / ``test_escalation_emit_omn13140.py``
(which disable this free-tier gate to isolate the escalation leg); here we prove
the retry leg and its composition with escalation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
    ModelLlmDelegationEscalationTriggeredEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationCompleted,
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

_COMPLETED_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"
_FAILED_TOPIC = "onex.evt.omnibase-infra.delegation-failed.v1"

# Deterministic escalation ladder for the composition tests.
_LADDER_NEXT: dict[str, str] = {"local": "cheap_cloud", "cheap_cloud": "claude"}
_LOCAL_RETRY_BUDGET = 2


@pytest.fixture
def retry_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``local`` is a free tier with a 2-retry budget; paid tiers escalate up the
    deterministic ladder. Isolates retry-local from live routing config."""
    monkeypatch.setattr(hw, "is_free_tier", lambda tier: tier == "local")
    monkeypatch.setattr(
        hw,
        "tier_max_retries",
        lambda tier: _LOCAL_RETRY_BUDGET if tier == "local" else 0,
    )
    monkeypatch.setattr(
        hw,
        "next_eligible_tier",
        lambda current, _excluded, **_kwargs: _LADDER_NEXT.get(current),
    )


def _make_request(
    cid: UUID, task_type: str = "code_generation"
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Repoint the import to TYPE_CHECKING.",
        task_type=task_type,  # type: ignore[arg-type]
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(
    cid: UUID,
    tier_name: str = "local",
    task_type: str = "code_generation",
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type=task_type,
        selected_model="Qwen3.6-35B-A3B",
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{tier_name}"),
        endpoint_url="http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-14234 reason="local AIPC LLM endpoint test fixture"
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a code generation assistant.",
        rationale=f"Task routed via tier '{tier_name}'.",
        tier_name=tier_name,
    )


def _make_inference_response(
    cid: UUID, content: str = "def f() -> int:\n    return 1"
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content=content,
        model_used="Qwen3.6-35B-A3B",
        latency_ms=42,
        prompt_tokens=100,
        completion_tokens=200,
        total_tokens=300,
    )


def _fail_gate(cid: UUID) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=cid,
        passed=False,
        quality_score=0.5,
        failure_reasons=("score_below_required_bar",),
        fallback_recommended=True,
    )


def _pass_gate(cid: UUID) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=cid,
        passed=True,
        quality_score=0.95,
        failure_reasons=(),
        fallback_recommended=False,
    )


def _drive_first_draft(
    handler: HandlerDelegationWorkflow, cid: UUID, tier_name: str = "local"
) -> None:
    """RECEIVED -> ROUTED -> INFERENCE_COMPLETED on ``tier_name``."""
    handler.handle_delegation_request(_make_request(cid))
    handler.handle_routing_decision(_make_routing_decision(cid, tier_name=tier_name))
    handler.handle_inference_response(_make_inference_response(cid))
    assert handler.workflows[cid].state == EnumDelegationState.INFERENCE_COMPLETED


def _redraft(
    handler: HandlerDelegationWorkflow, cid: UUID, tier_name: str = "local"
) -> None:
    """After a retry intent (state ROUTED, routing_decision cleared), feed the SAME
    tier's fresh routing decision + inference so the next gate can be evaluated."""
    assert handler.workflows[cid].state == EnumDelegationState.ROUTED
    handler.handle_routing_decision(_make_routing_decision(cid, tier_name=tier_name))
    handler.handle_inference_response(_make_inference_response(cid))
    assert handler.workflows[cid].state == EnumDelegationState.INFERENCE_COMPLETED


@pytest.mark.unit
class TestRetryLocalBusPath:
    def test_first_free_tier_gate_fail_retries_same_tier(
        self, retry_local_env: None
    ) -> None:
        """A sub-bar draft on a free tier re-routes to the SAME tier — no escalation
        event, no escalation_count bump, state back to ROUTED."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _drive_first_draft(handler, cid)

        intents = handler.handle_gate_result(_fail_gate(cid))

        routing = [i for i in intents if isinstance(i, ModelRoutingIntent)]
        assert len(routing) == 1
        # Re-route pins the SAME free tier, not a higher one.
        assert routing[0].min_tier_name == "local"
        assert not any(
            isinstance(i, ModelLlmDelegationEscalationTriggeredEvent) for i in intents
        )
        wf = handler.workflows[cid]
        assert wf.state == EnumDelegationState.ROUTED
        assert wf.escalation_count == 0
        assert wf.local_retry_count == 1

    def test_retry_then_pass_completes_local_at_zero_cost(
        self, retry_local_env: None
    ) -> None:
        """Best-of-N: a later local draft that clears the gate completes on local
        with escalation_count 0 and $0 measured cost (free tier)."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _drive_first_draft(handler, cid)
        handler.handle_gate_result(_fail_gate(cid))  # draft 1 fails -> retry
        _redraft(handler, cid)  # draft 2

        events = handler.handle_gate_result(_pass_gate(cid))

        wf = handler.workflows[cid]
        assert wf.state == EnumDelegationState.COMPLETED
        assert wf.escalation_count == 0
        terminal = next(e for e in events if isinstance(e, ModelDelegationResult))
        assert isinstance(terminal, ModelDelegationCompleted)
        assert terminal.quality_passed is True
        # local is free_local in the real cost model -> $0 across every draft.
        assert terminal.cumulative_attempt_cost == 0.0

    def test_budget_exhausted_then_escalates_to_paid(
        self, retry_local_env: None
    ) -> None:
        """After ``max_retries`` local drafts all fail, the workflow escalates to the
        next (paid) tier — escalation_count increments and an escalation event fires,
        and every failed local draft is recorded in escalation_history."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()

        _drive_first_draft(handler, cid)
        i1 = handler.handle_gate_result(_fail_gate(cid))  # draft 1 -> retry
        assert i1[0].min_tier_name == "local"
        assert handler.workflows[cid].local_retry_count == 1
        assert handler.workflows[cid].escalation_count == 0

        _redraft(handler, cid)
        i2 = handler.handle_gate_result(_fail_gate(cid))  # draft 2 -> retry
        assert i2[0].min_tier_name == "local"
        assert handler.workflows[cid].local_retry_count == 2
        assert handler.workflows[cid].escalation_count == 0

        _redraft(handler, cid)
        i3 = handler.handle_gate_result(
            _fail_gate(cid)
        )  # draft 3 -> budget out -> escalate

        routing = [i for i in i3 if isinstance(i, ModelRoutingIntent)]
        assert routing
        assert routing[0].min_tier_name == "cheap_cloud"
        assert any(
            isinstance(i, ModelLlmDelegationEscalationTriggeredEvent) for i in i3
        )
        wf = handler.workflows[cid]
        assert wf.escalation_count == 1
        local_attempts = [a for a in wf.escalation_history if a.tier_name == "local"]
        assert len(local_attempts) == 3  # 1 initial + 2 retries, all recorded

    def test_paid_tier_gate_fail_escalates_without_retry(
        self, retry_local_env: None
    ) -> None:
        """Fail-closed: a sub-bar draft on a PAID tier escalates immediately (up the
        ladder) — retry-local never applies to a non-free tier."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _drive_first_draft(handler, cid, tier_name="cheap_cloud")

        intents = handler.handle_gate_result(_fail_gate(cid))

        routing = [i for i in intents if isinstance(i, ModelRoutingIntent)]
        assert routing
        assert routing[0].min_tier_name == "claude"
        wf = handler.workflows[cid]
        assert wf.local_retry_count == 0
        assert wf.escalation_count == 1
