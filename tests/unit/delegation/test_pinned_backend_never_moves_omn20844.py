# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A caller-pinned delegation runs on the pinned backend or fails typed (OMN-20844).

The pin used to select the INITIAL attempt only. A transport failure on the
pinned backend then took a same-tier sibling hop (OMN-14402), and a sub-bar
gate result took the ordinary escalation ladder, so a run pinned to one
backend was answered by another under the pin's name. Measured on the rung
benchmark: 17 of 26 test-class cells pinned to openrouter-nemotron-super were
answered by openrouter-nemotron-ultra after the pinned call timed out, and 4
cells pinned to ultra were answered by super.

These tests hold the bus path (``HandlerDelegationWorkflow`` and the routing
reducer's ``delta``) and the escalation COMPUTE to one rule: a pinned run never
re-routes to a backend other than the pin. Where the unpinned run would move,
the pinned run terminates with ``pinned_backend_failed``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.nodes.node_delegation_escalation_decision_compute.handlers.handler_escalation_decision import (
    HandlerEscalationDecision,
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
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.routing.model_escalation_decision_request import (
    ModelEscalationDecisionRequest,
)

pytestmark = pytest.mark.usefixtures("stub_provider_quota_reader")

_PIN = "openrouter-nemotron-super"
_SIBLING = "openrouter-nemotron-ultra"
_TIER = "cheap_frontier"


# ---------------------------------------------------------------------------
# Orchestrator workflow (the deployed-lane path)
# ---------------------------------------------------------------------------


@pytest.fixture
def frontier_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin's tier declares an untried sibling and a higher tier exists, so
    every move the unpinned run makes is available to the pinned one too."""

    def _sibling(tier: str, _task_type: str, excluded: frozenset[str]) -> str | None:
        if tier != _TIER:
            return None
        return next((b for b in (_PIN, _SIBLING) if b not in excluded), None)

    monkeypatch.setattr(hw, "sibling_backend_available_in_tier", _sibling)
    monkeypatch.setattr(
        hw,
        "next_eligible_tier",
        lambda current, _excluded, **_kwargs: "claude" if current == _TIER else None,
    )
    monkeypatch.setattr(hw, "is_free_tier", lambda _tier: False)


def _request(cid: UUID, *, backend_id: str | None) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write pytest tests for the function below.",
        task_type="test",
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
        backend_id=backend_id,
    )


def _decision(cid: UUID, backend_ref: str) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="test",
        selected_model=f"{backend_ref}-model",
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{backend_ref}"),
        endpoint_url=f"https://{backend_ref}.test/v1/chat/completions",
        cost_tier="low",
        max_context_tokens=32768,
        max_tokens=8192,
        system_prompt="You write tests.",
        rationale=f"Task routed via tier '{_TIER}' to {backend_ref}.",
        tier_name=_TIER,
        selected_backend_ref=backend_ref,
    )


def _timeout(cid: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content="",
        model_used=f"{_PIN}-model",
        latency_ms=120_000,
        prompt_tokens=600,
        completion_tokens=0,
        total_tokens=600,
        error_message="provider call timed out after 120.000s",
    )


def _routing_intents(events: list[Any]) -> list[ModelRoutingIntent]:
    return [e for e in events if isinstance(e, ModelRoutingIntent)]


def _failed(events: list[Any]) -> ModelDelegationFailed:
    return next(e for e in events if isinstance(e, ModelDelegationFailed))


@pytest.mark.unit
def test_unpinned_transport_failure_moves_to_sibling(frontier_ladder: None) -> None:
    """Positive control: without a pin the same failure takes the sibling hop."""
    handler = HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(_request(cid, backend_id=None))
    handler.handle_routing_decision(_decision(cid, _PIN))

    events = handler.handle_inference_response(_timeout(cid))

    assert len(_routing_intents(events)) == 1


@pytest.mark.unit
def test_pinned_transport_failure_fails_typed_instead_of_moving(
    frontier_ladder: None,
) -> None:
    handler = HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(_request(cid, backend_id=_PIN))
    handler.handle_routing_decision(_decision(cid, _PIN))

    events = handler.handle_inference_response(_timeout(cid))

    assert _routing_intents(events) == [], "a pinned run must not re-route"
    failed = _failed(events)
    assert failed.terminal_failure_reason == "pinned_backend_failed"
    assert handler.workflows[cid].state == EnumDelegationState.FAILED
    assert handler.workflows[cid].escalation_count == 0


@pytest.mark.unit
def test_pinned_gate_failure_fails_typed_instead_of_escalating(
    frontier_ladder: None,
) -> None:
    handler = HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(_request(cid, backend_id=_PIN))
    handler.handle_routing_decision(_decision(cid, _PIN))
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="def test_x():\n    assert True\n",
            model_used=f"{_PIN}-model",
            latency_ms=42,
            prompt_tokens=100,
            completion_tokens=200,
            total_tokens=300,
        )
    )

    events = handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=cid,
            passed=False,
            quality_score=0.5,
            failure_reasons=("score_below_required_bar",),
            fallback_recommended=True,
        )
    )

    assert _routing_intents(events) == [], "a pinned run must not escalate"
    assert _failed(events).terminal_failure_reason == "pinned_backend_failed"


# ---------------------------------------------------------------------------
# Escalation decision COMPUTE
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_refuses_escalation_for_a_pinned_backend() -> None:
    result = HandlerEscalationDecision().handle(
        ModelEscalationDecisionRequest(
            escalation_count=0,
            max_escalation_attempts=2,
            current_tier_name=_TIER,
            error_retryable=True,
            next_tier_name="claude",
            pinned_backend_ref=_PIN,
        )
    )

    assert result.can_escalate is False
    assert result.next_tier_name is None
    assert result.terminal_failure_reason == "pinned_backend_failed"


@pytest.mark.unit
def test_compute_keeps_non_retryable_reason_for_a_pinned_backend() -> None:
    result = HandlerEscalationDecision().handle(
        ModelEscalationDecisionRequest(
            escalation_count=0,
            max_escalation_attempts=2,
            current_tier_name=_TIER,
            error_retryable=False,
            non_retryable_reason="non_retryable_inference_response",
            pinned_backend_ref=_PIN,
        )
    )

    assert result.terminal_failure_reason == "non_retryable_inference_response"


# ---------------------------------------------------------------------------
# Routing reducer
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned_reducer(monkeypatch: pytest.MonkeyPatch) -> None:
    config = parse_delegation_config_yaml(
        f"""
        tiers:
          - name: local
            cost_per_1k_tokens: 0.0
            models:
              - id: local-model
                backend_id: local-primary
                max_context_tokens: 32768
                use_for: [test]
          - name: {_TIER}
            cost_per_1k_tokens: 0.0
            models:
              - id: {_SIBLING}-model
                backend_id: {_SIBLING}
                max_context_tokens: 32768
                use_for: [test]
              - id: {_PIN}-model
                backend_id: {_PIN}
                max_context_tokens: 32768
                use_for: [test]
          - name: claude
            cost_per_1k_tokens: 0.003
            models:
              - id: claude-model
                backend_id: claude-fallback
                max_context_tokens: 32768
                use_for: [test]
        """
    )
    task_contract: dict[str, object] = {
        "task_classes": {
            "test": {
                "cloud_routing_policy": "allowed",
                "pricing_ceiling_per_1k_tokens": 0.003,
                "escalation_policy": {
                    "max_escalations": 2,
                    "tier_order": ["local", _TIER, "claude"],
                },
            }
        }
    }
    backends = {
        ref: routing.BifrostBackendRef(
            provider="openai_compatible",
            endpoint_url=f"https://{ref}.test/v1/chat/completions",
            model_name=f"{ref}-model",
            timeout_ms=30_000,
            max_tokens=8192,
        )
        for ref in ("local-primary", _SIBLING, _PIN, "claude-fallback")
    }
    monkeypatch.setattr(routing, "_get_config", lambda: config)
    monkeypatch.setattr(routing, "_get_task_class_contract", lambda: task_contract)
    monkeypatch.setattr(routing, "_load_bifrost_endpoints", lambda: backends)
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _backend: True)


@pytest.mark.unit
def test_same_tier_reroute_of_a_pinned_request_stays_on_the_pin(
    pinned_reducer: None,
) -> None:
    decision = routing.delta(_request(uuid4(), backend_id=_PIN), min_tier_name=_TIER)

    assert decision.selected_backend_ref == _PIN


@pytest.mark.unit
def test_reroute_past_the_pins_tier_refuses_instead_of_moving(
    pinned_reducer: None,
) -> None:
    with pytest.raises(ProtocolConfigurationError, match=_PIN):
        routing.delta(_request(uuid4(), backend_id=_PIN), min_tier_name="claude")


@pytest.mark.unit
def test_reroute_with_the_pin_excluded_refuses_instead_of_taking_a_sibling(
    pinned_reducer: None,
) -> None:
    with pytest.raises(ProtocolConfigurationError, match=_PIN):
        routing.delta(
            _request(uuid4(), backend_id=_PIN),
            min_tier_name=_TIER,
            excluded_backend_refs=frozenset({_PIN}),
        )
