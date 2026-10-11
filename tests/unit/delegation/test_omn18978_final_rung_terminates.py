# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18978: the rung a ladder ends on is recorded as TERMINATE, never CLIMB.

A rung is recorded when it is judged, before the escalation decision runs. A
rung refused at the top of the ladder (no higher tier, or the escalation
budget spent) was therefore recorded ``climb`` on a run that ended on it: a
decision to move on that no rung carried out. The enum already has the value
for a ladder that stops (``terminate``, OMN-19016) but only the shape-veto
branch ever used it.

The shapes are rebuilt through the real orchestrator FSM; only the escalation
verdict is controlled, because it is the input under test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from pydantic import BaseModel

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationFailed,
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
from omnimarket.routing.model_escalation_decision_result import (
    ModelEscalationDecisionResult,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

_TIMEOUT = "The read operation timed out while calling provider [ReadTimeout]"
_ANSWER = "def test_answer():\n    assert 1 + 1 == 2"


def _route(cid: UUID, tier: str, backend: str) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="test",
        selected_model=backend,
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{backend}"),
        selected_backend_ref=backend,
        endpoint_url="http://provider.invalid:8000",
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale=f"Fixture route to {tier}/{backend}",
        tier_name=tier,
    )


@pytest.fixture
def ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[hw.HandlerDelegationWorkflow, UUID]:
    """A workflow with no same-tier retry or sibling, so each rung is one attempt."""
    handler = hw.HandlerDelegationWorkflow(workflows={})
    monkeypatch.setattr(hw, "is_free_tier", lambda _tier: False)
    monkeypatch.setattr(hw, "tier_max_retries", lambda _tier: 0)
    monkeypatch.setattr(hw, "sibling_backend_available_in_tier", lambda *_a: None)
    cid = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt="Write unit tests for verify_registration.py",
            task_type="test",
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
        )
    )
    return handler, cid


def _verdict(
    monkeypatch: pytest.MonkeyPatch,
    handler: hw.HandlerDelegationWorkflow,
    *,
    next_tier: str | None,
) -> None:
    """Fix the escalation verdict: climb to ``next_tier``, or end the ladder."""
    result = (
        ModelEscalationDecisionResult(can_escalate=True, next_tier_name=next_tier)
        if next_tier is not None
        else ModelEscalationDecisionResult(
            can_escalate=False, terminal_failure_reason="no_higher_tier_available"
        )
    )
    monkeypatch.setattr(handler, "_decide_escalation", lambda *_a, **_kw: result)


def _gate_refusal(
    handler: hw.HandlerDelegationWorkflow, cid: UUID, tier: str
) -> list[BaseModel]:
    handler.handle_routing_decision(_route(cid, tier, f"{tier}-backend"))
    contract = handler.workflows[cid].effective_deliverable_contract
    assert contract is not None
    assert contract.render_start_marker is not None
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content=f"{contract.render_start_marker}\n{_ANSWER}",
            model_used=f"{tier}-backend",
            latency_ms=10,
        )
    )
    return handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=cid,
            passed=False,
            quality_score=0.5,
            fail_category="fail_heuristic",
            failure_reasons=("score_below_required_bar",),
            fallback_recommended=True,
            finish_reason=EnumProviderFinishReason.STOP,
        )
    )


def _provider_failure(
    handler: hw.HandlerDelegationWorkflow, cid: UUID, tier: str
) -> list[BaseModel]:
    handler.handle_routing_decision(_route(cid, tier, f"{tier}-backend"))
    return handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="",
            model_used=f"{tier}-backend",
            latency_ms=10,
            error_message=_TIMEOUT,
        )
    )


def _failed(events: list[BaseModel]) -> ModelDelegationFailed:
    failed = [event for event in events if isinstance(event, ModelDelegationFailed)]
    assert len(failed) == 1
    return failed[0]


def _decisions(failed: ModelDelegationFailed) -> list[tuple[str, str]]:
    history = failed.model_dump(mode="json")["escalation_history"]
    return [
        (attempt["acceptance_decision"], attempt["acceptance_reason"])
        for attempt in history
    ]


def test_a_gate_refused_top_rung_is_recorded_terminate(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rung one is refused and climbs; rung two is refused with nowhere to go."""
    handler, cid = ladder
    _verdict(monkeypatch, handler, next_tier="cheap_cloud")
    first = _gate_refusal(handler, cid, "local")
    assert any(isinstance(event, ModelRoutingIntent) for event in first)

    _verdict(monkeypatch, handler, next_tier=None)
    failed = _failed(_gate_refusal(handler, cid, "cheap_cloud"))

    assert _decisions(failed) == [
        ("climb", "acceptance_criteria_failed"),
        ("terminate", "acceptance_criteria_failed"),
    ]
    assert failed.terminal_failure_cause is not None
    assert failed.terminal_failure_cause.value == "quality_gate_refused"


def test_a_provider_failed_top_rung_is_recorded_terminate(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A top rung whose call failed with no higher tier ended the run too."""
    handler, cid = ladder
    _verdict(monkeypatch, handler, next_tier="cheap_cloud")
    first = _provider_failure(handler, cid, "local")
    assert any(isinstance(event, ModelRoutingIntent) for event in first)

    _verdict(monkeypatch, handler, next_tier=None)
    failed = _failed(_provider_failure(handler, cid, "cheap_cloud"))

    assert _decisions(failed) == [
        ("climb", "provider_call_failed"),
        ("terminate", "provider_call_failed"),
    ]


def test_a_rung_that_climbs_stays_recorded_climb(
    ladder: tuple[hw.HandlerDelegationWorkflow, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: only the rung the run ended on changes."""
    handler, cid = ladder
    _verdict(monkeypatch, handler, next_tier="cheap_cloud")
    events = _gate_refusal(handler, cid, "local")

    assert not any(isinstance(event, ModelDelegationFailed) for event in events)
    history = handler.workflows[cid].escalation_history
    assert [attempt.acceptance_decision.value for attempt in history] == ["climb"]
