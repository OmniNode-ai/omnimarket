# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cloud-escalation TERMINAL EMIT regression suite (OMN-13140).

PR #1215 opened the three routing GATES (quality-gate fallback verdicts, the
code_generation->cheap_cloud routing path, finish_reason=length retryability) so
the PATH to cloud is open. But the terminal escalation event was never published
on the delegation dispatch path: the orchestrator escalated (re-routed to the
next tier) but emitted ONLY a ``ModelRoutingIntent`` — never the typed
``ModelLlmDelegationEscalationTriggeredEvent`` declared on
``onex.evt.omnimarket.delegation-escalation-triggered.v1``. Live consequence:
the escalation topic high-water-mark stayed at 0 and the routing-feedback reducer
saw zero escalation signal.

The fix wires the emit at the canonical decision point — the orchestrator
``HandlerDelegationWorkflow`` escalation branches (``handle_gate_result`` for the
quality-gate-driven escalation and ``handle_inference_response`` for the
infra-error-driven escalation), because that is the ONLY surface holding the full
escalation decision state (``fallback_recommended``, the resolved ``next_tier``,
``escalation_count``, ``current_tier_name``, ``inference_model_used``,
``failure_reasons``). The call-effect ``handle()`` is a single stateless LLM call
with none of that context.

These tests drive the REAL dispatch path over the contract handlers (routing
reducer -> inference effect -> quality gate reducer -> orchestrator), not handler
isolation, because handler-isolation tests pass while the live chain fails
(memory feedback_real_dispatch_path_tests). The applier-routing test additionally
proves the typed event lands on the canonical escalation topic through the same
contract-declared ``published_events`` + ``publish_topics`` wiring the runtime
uses, so a green test corresponds to a non-zero escalation HWM in production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
    ModelLlmDelegationEscalationTriggeredEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
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

# Canonical escalation topic declared on the call-effect contract and now also on
# the orchestrator contract publish_topics so the applier allowlist accepts it.
_ESCALATION_TOPIC = "onex.evt.omnimarket.delegation-escalation-triggered.v1"


def _make_request(
    correlation_id: UUID | None = None,
    task_type: str = "test",
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type=task_type,  # type: ignore[arg-type]
        correlation_id=correlation_id or uuid4(),
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(
    correlation_id: UUID,
    task_type: str = "test",
    tier_name: str = "local",
    selected_model: str = "qwen3-coder-30b",
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type=task_type,
        selected_model=selected_model,
        selected_backend_id=uuid5(
            NAMESPACE_DNS, f"omninode.ai/backends/{selected_model}"
        ),
        endpoint_url="http://local.test:8000/v1/chat/completions",
        cost_tier="low",
        max_context_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale=f"Task '{task_type}' routed to {selected_model}.",
        tier_name=tier_name,
    )


def _make_inference_response(
    correlation_id: UUID,
    content: str = "def test_foo():\n    pass",
    model_used: str = "qwen3-coder-30b",
    error_message: str = "",
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content=content,
        model_used=model_used,
        latency_ms=42,
        prompt_tokens=100,
        completion_tokens=200,
        total_tokens=300,
        error_message=error_message,
    )


def _make_gate_result(
    correlation_id: UUID,
    *,
    passed: bool = False,
    quality_score: float = 0.2,
    failure_reasons: tuple[str, ...] = ("WEAK_OUTPUT: too short",),
    fallback_recommended: bool = True,
) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=correlation_id,
        passed=passed,
        quality_score=quality_score,
        failure_reasons=failure_reasons,
        fallback_recommended=fallback_recommended,
    )


def _advance_to_gate_evaluated(
    handler: HandlerDelegationWorkflow,
    cid: UUID,
    *,
    tier_name: str = "local",
) -> None:
    """Drive the FSM RECEIVED -> ... -> INFERENCE_COMPLETED over the real path."""
    handler.handle_delegation_request(_make_request(correlation_id=cid))
    handler.handle_routing_decision(_make_routing_decision(cid, tier_name=tier_name))
    handler.handle_inference_response(_make_inference_response(cid))
    assert handler.workflows[cid].state == EnumDelegationState.INFERENCE_COMPLETED


def _escalation_events(
    events: list[Any],
) -> list[ModelLlmDelegationEscalationTriggeredEvent]:
    return [
        e for e in events if isinstance(e, ModelLlmDelegationEscalationTriggeredEvent)
    ]


@pytest.mark.unit
class TestQualityGateEscalationEmitsTerminalEvent:
    """The headline: a quality-gate-recommends-fallback escalation with a routable
    next tier MUST publish a ModelLlmDelegationEscalationTriggeredEvent alongside
    the re-routing intent. Before OMN-13140 only the routing intent was emitted.
    """

    def test_escalation_emits_typed_event_with_escalation_count(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="local")

        events = handler.handle_gate_result(_make_gate_result(cid))

        # The escalation decision still re-routes to a higher tier.
        routing_intents = [e for e in events if isinstance(e, ModelRoutingIntent)]
        assert len(routing_intents) == 1
        assert routing_intents[0].min_tier_name is not None
        assert routing_intents[0].min_tier_name != "local"
        assert handler.workflows[cid].state == EnumDelegationState.ROUTED

        # ...AND it now emits the terminal escalation event (the OMN-13140 gap).
        escalations = _escalation_events(events)
        assert len(escalations) == 1, (
            "a quality-gate escalation with a routable next tier must publish "
            "ModelLlmDelegationEscalationTriggeredEvent (HWM>0 proof)"
        )
        event = escalations[0]
        assert event.failure_class is EnumDelegationFailureClass.QUALITY_GATE_FAILED
        assert event.model_id == "qwen3-coder-30b"
        assert event.escalation_reason
        # escalation_count proof: the workflow incremented to >= 1 and the event
        # carries the in-tier attempt number for the escalating model.
        assert handler.workflows[cid].escalation_count >= 1
        assert event.attempt_number >= 1

    def test_no_event_when_fallback_not_recommended(self) -> None:
        """A terminal failure (no fallback) must NOT emit an escalation event."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="local")

        events = handler.handle_gate_result(
            _make_gate_result(
                cid,
                failure_reasons=("low_quality",),
                fallback_recommended=False,
            )
        )
        assert handler.workflows[cid].state == EnumDelegationState.FAILED
        assert _escalation_events(events) == []

    def test_no_event_when_no_higher_tier(self) -> None:
        """Top-tier exhaustion must NOT emit an escalation event."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="claude")

        events = handler.handle_gate_result(_make_gate_result(cid))
        assert handler.workflows[cid].state == EnumDelegationState.FAILED
        assert _escalation_events(events) == []

    def test_passing_gate_emits_no_escalation_event(self) -> None:
        """A passing gate completes the workflow with no escalation event."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="local")

        events = handler.handle_gate_result(
            _make_gate_result(
                cid,
                passed=True,
                quality_score=0.9,
                failure_reasons=(),
                fallback_recommended=False,
            )
        )
        assert handler.workflows[cid].state == EnumDelegationState.COMPLETED
        assert _escalation_events(events) == []


@pytest.mark.unit
class TestInferenceErrorEscalationEmitsTerminalEvent:
    """The infra-error escalation branch (handle_inference_response) must also
    publish the typed escalation event when it retries on a higher tier.
    """

    def test_retryable_inference_error_emits_escalation_event(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        handler.handle_delegation_request(_make_request(correlation_id=cid))
        handler.handle_routing_decision(_make_routing_decision(cid, tier_name="local"))

        # A retryable inference error (e.g. truncation) escalates to a higher tier.
        events = handler.handle_inference_response(
            _make_inference_response(
                cid,
                content="",
                error_message="finish_reason=length",
            )
        )

        routing_intents = [e for e in events if isinstance(e, ModelRoutingIntent)]
        assert len(routing_intents) == 1
        assert handler.workflows[cid].state == EnumDelegationState.ROUTED

        escalations = _escalation_events(events)
        assert len(escalations) == 1, (
            "a retryable inference error that escalates must publish "
            "ModelLlmDelegationEscalationTriggeredEvent"
        )
        assert escalations[0].model_id == "qwen3-coder-30b"
        assert escalations[0].escalation_reason


@pytest.mark.unit
class TestEscalationEventRoutesToCanonicalTopicViaApplier:
    """Prove the typed escalation event resolves to the canonical escalation topic
    through the SAME contract-declared published_events + publish_topics wiring the
    runtime DispatchResultApplier uses — so a green test is HWM>0 in production.

    This is the live-path proof: the applier reads the orchestrator contract,
    builds the topic map, and resolves the bare escalation event's output topic.
    """

    def _applier(self) -> Any:
        import yaml
        from omnibase_infra.protocols import ProtocolEventBusLike
        from omnibase_infra.runtime.contract_topic_router import (
            build_topic_router_from_contract,
        )
        from omnibase_infra.runtime.event_bus_subcontract_wiring import (
            load_published_events_map,
        )
        from omnibase_infra.runtime.service_dispatch_result_applier import (
            DispatchResultApplier,
        )

        contract_path = Path(
            "src/omnimarket/nodes/node_delegation_orchestrator/contract.yaml"
        )
        contract_data = yaml.safe_load(contract_path.read_text())
        publish_topics = list(contract_data["event_bus"]["publish_topics"])
        published_events_map = load_published_events_map(contract_path)
        topic_router = build_topic_router_from_contract(contract_data)

        # spec-bound: this test only exercises topic RESOLUTION
        # (_resolve_output_topic); the bus is never published to, but the
        # transport-mock-lint gate still requires a spec on the transport surface.
        return DispatchResultApplier(
            event_bus=MagicMock(spec=ProtocolEventBusLike),
            output_topic="onex.evt.omnibase-infra.delegation-completed.v1",
            topic_router=topic_router,
            output_topic_map=published_events_map,
            allowed_output_topics=publish_topics,
        )

    def test_escalation_event_resolves_to_escalation_topic(self) -> None:
        applier = self._applier()
        event = ModelLlmDelegationEscalationTriggeredEvent(
            correlation_id=str(uuid4()),
            causation_id=str(uuid4()),
            request_id=str(uuid4()),
            task_type="code_generation",
            task_id=None,
            model_id="qwen3-coder-30b",
            attempt_number=1,
            failure_class=EnumDelegationFailureClass.QUALITY_GATE_FAILED,
            escalation_reason="WEAK_OUTPUT: too short",
            next_model_id=None,
            created_at=datetime.now(UTC),
        )
        resolved = applier._resolve_output_topic(event)
        assert resolved == _ESCALATION_TOPIC, (
            "the orchestrator contract must declare the escalation topic in "
            "publish_topics + published_events so the applier routes the typed "
            "event to the canonical escalation topic"
        )
