# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests verifying delegation dispatchers publish to the event bus.

Acceptance criteria for OMN-10812: a mock event bus receives publish_envelope()
calls with the correct topic when DispatcherQualityGateResult and
DispatcherAgentTaskLifecycle fire and produce terminal delegation events.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import NAMESPACE_DNS, uuid4, uuid5

import pytest
from omnibase_core.enums import EnumInvocationKind
from omnibase_core.enums.enum_agent_protocol import EnumAgentProtocol
from omnibase_core.enums.enum_agent_task_lifecycle_type import (
    EnumAgentTaskLifecycleType,
)
from omnibase_core.models.delegation.model_agent_task_lifecycle_event import (
    ModelAgentTaskLifecycleEvent,
)
from omnibase_core.models.delegation.model_invocation_command import (
    ModelInvocationCommand,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumDispatchStatus
from omnibase_infra.errors import InfraUnavailableError
from omnibase_infra.event_bus.topic_constants import (
    TOPIC_DELEGATION_COMPLETED,
    TOPIC_DELEGATION_FAILED,
    TOPIC_DELEGATION_TASK_DELEGATED,
)

from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_agent_task_lifecycle import (
    DispatcherAgentTaskLifecycle,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_quality_gate_result import (
    DispatcherQualityGateResult,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_ENDPOINT_URL = "http://delegation-llm.test:8000"


def _make_mock_bus() -> MagicMock:
    bus = MagicMock()
    bus.publish_envelope = AsyncMock()
    return bus


def _make_envelope(
    payload: object, correlation_id: object
) -> ModelEventEnvelope[object]:
    return ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=payload,
        correlation_id=correlation_id,  # type: ignore[arg-type]
        envelope_timestamp=datetime.now(UTC),
    )


def _run_workflow_to_gate(
    handler: HandlerDelegationWorkflow,
    *,
    passed: bool = True,
) -> tuple[object, object]:
    """Drive FSM to INFERENCE_COMPLETED, return (cid, gate_result_envelope)."""
    cid = uuid4()

    _start_delegation(handler, cid)

    response = ModelInferenceResponseData(
        correlation_id=cid,
        content="def test_x(): pass" if passed else "I cannot help.",
        model_used="Qwen3-Coder-30B-A3B",
        llm_call_id="chatcmpl-test" if passed else "chatcmpl-fail",
        latency_ms=100 if passed else 50,
        prompt_tokens=50 if passed else 30,
        completion_tokens=20 if passed else 10,
        total_tokens=70 if passed else 40,
    )
    handler.handle_inference_response(response)

    gate_result = ModelQualityGateResult(
        correlation_id=cid,
        passed=passed,
        quality_score=0.9 if passed else 0.1,
        failure_reasons=() if passed else ("REFUSAL",),
        fallback_recommended=not passed,
    )
    gate_envelope = _make_envelope(gate_result, cid)
    return cid, gate_envelope


def _start_delegation(
    handler: HandlerDelegationWorkflow,
    cid: object,
) -> None:
    request = ModelDelegationRequest(
        prompt="Write tests",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=cid,  # type: ignore[arg-type]
        emitted_at=datetime.now(UTC),
    )
    handler.handle_delegation_request(request)

    decision = ModelRoutingDecision(
        correlation_id=cid,  # type: ignore[arg-type]
        task_type="test",
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(
            NAMESPACE_DNS, "omninode.ai/backends/qwen3-coder-30b"
        ),
        endpoint_url=TEST_ENDPOINT_URL,
        cost_tier="low",
        max_context_tokens=65536,
        system_prompt="You are an assistant.",
        rationale="Routing test.",
    )
    handler.handle_routing_decision(decision)


def _start_agent_invocation(
    handler: HandlerDelegationWorkflow,
    cid: object,
) -> None:
    request = ModelDelegationRequest(
        prompt="Write tests",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=cid,  # type: ignore[arg-type]
        emitted_at=datetime.now(UTC),
    )
    handler.handle_delegation_request(request)

    command = ModelInvocationCommand(
        task_id=uuid4(),
        correlation_id=cid,  # type: ignore[arg-type]
        target_ref="agent://remote",
        invocation_kind=EnumInvocationKind.AGENT,
        agent_protocol=EnumAgentProtocol.A2A,
    )
    handler.handle_invocation_command(command)


# ---------------------------------------------------------------------------
# DispatcherQualityGateResult — bus publish assertion
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestDispatcherQualityGateResultBusPublish:
    """DispatcherQualityGateResult calls publish_envelope on the bus for terminal events."""

    async def test_publish_envelope_called_for_delegation_completed(self) -> None:
        bus = _make_mock_bus()
        handler = HandlerDelegationWorkflow()
        dispatcher = DispatcherQualityGateResult(handler, event_bus=bus)  # type: ignore[arg-type]

        _cid, gate_envelope = _run_workflow_to_gate(handler)
        result = await dispatcher.handle(gate_envelope)

        assert result.status == EnumDispatchStatus.SUCCESS
        assert bus.publish_envelope.called

        published_topics = [
            c.kwargs["topic"] for c in bus.publish_envelope.call_args_list
        ]
        assert TOPIC_DELEGATION_COMPLETED in published_topics
        assert TOPIC_DELEGATION_TASK_DELEGATED in published_topics

    async def test_output_events_empty_when_bus_wired(self) -> None:
        """When bus is wired, routable events (with .topic) are published directly.

        Events without a .topic attribute (e.g. ModelBaselineIntent) are not
        routable via the direct-publish path and remain in output_events for the
        DispatchResultApplier's type-based router.  Only topicless events may appear.
        """
        bus = _make_mock_bus()
        handler = HandlerDelegationWorkflow()
        dispatcher = DispatcherQualityGateResult(handler, event_bus=bus)  # type: ignore[arg-type]

        _cid, gate_envelope = _run_workflow_to_gate(handler)
        result = await dispatcher.handle(gate_envelope)

        assert result.status == EnumDispatchStatus.SUCCESS
        # All events with a .topic attribute must have been published directly.
        for event in result.output_events:
            assert not hasattr(event, "topic") or event.topic is None  # type: ignore[union-attr]

    async def test_output_events_populated_when_no_bus(self) -> None:
        """When no bus, events go into output_events for the DispatchResultApplier."""
        handler = HandlerDelegationWorkflow()
        dispatcher = DispatcherQualityGateResult(handler, event_bus=None)

        _cid, gate_envelope = _run_workflow_to_gate(handler)
        result = await dispatcher.handle(gate_envelope)

        assert result.status == EnumDispatchStatus.SUCCESS
        assert len(result.output_events) > 0

    async def test_publish_envelope_called_for_delegation_failed(self) -> None:
        bus = _make_mock_bus()
        handler = HandlerDelegationWorkflow()
        dispatcher = DispatcherQualityGateResult(handler, event_bus=bus)  # type: ignore[arg-type]

        _cid, gate_envelope = _run_workflow_to_gate(handler, passed=False)
        result = await dispatcher.handle(gate_envelope)

        assert result.status == EnumDispatchStatus.SUCCESS
        published_topics = [
            c.kwargs["topic"] for c in bus.publish_envelope.call_args_list
        ]
        assert TOPIC_DELEGATION_FAILED in published_topics
        assert TOPIC_DELEGATION_TASK_DELEGATED in published_topics

    async def test_envelope_payload_matches_expected_topic(self) -> None:
        """Each publish_envelope call carries the correct event in the envelope payload."""
        bus = _make_mock_bus()
        handler = HandlerDelegationWorkflow()
        dispatcher = DispatcherQualityGateResult(handler, event_bus=bus)  # type: ignore[arg-type]

        _cid, gate_envelope = _run_workflow_to_gate(handler)
        await dispatcher.handle(gate_envelope)

        calls = bus.publish_envelope.call_args_list
        topic_to_payload_type = {
            c.kwargs["topic"]: type(c.args[0].payload).__name__ for c in calls
        }
        # The envelope payload is ModelDelegationEvent (the wrapper); its .payload
        # field is ModelDelegationResult.  Checking the outer type is sufficient.
        assert (
            topic_to_payload_type.get(TOPIC_DELEGATION_COMPLETED)
            == "ModelDelegationEvent"
        )
        assert (
            topic_to_payload_type.get(TOPIC_DELEGATION_TASK_DELEGATED)
            == "ModelTaskDelegatedEvent"
        )

    async def test_direct_publish_infra_failure_records_circuit_failure(
        self,
    ) -> None:
        """Direct bus outages must advance the dispatcher's circuit breaker."""
        bus = _make_mock_bus()
        bus.publish_envelope.side_effect = InfraUnavailableError(
            "event bus unavailable"
        )
        handler = HandlerDelegationWorkflow()
        dispatcher = DispatcherQualityGateResult(handler, event_bus=bus)  # type: ignore[arg-type]

        _cid, gate_envelope = _run_workflow_to_gate(handler)
        result = await dispatcher.handle(gate_envelope)

        assert result.status == EnumDispatchStatus.HANDLER_ERROR
        assert dispatcher._circuit_breaker_failures == 1


# ---------------------------------------------------------------------------
# DispatcherAgentTaskLifecycle — bus publish assertion
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestDispatcherAgentTaskLifecycleBusPublish:
    """DispatcherAgentTaskLifecycle calls publish_envelope for terminal lifecycle events."""

    def _make_completed_lifecycle_envelope(
        self, cid: object
    ) -> ModelEventEnvelope[object]:
        lifecycle_event = ModelAgentTaskLifecycleEvent(
            task_id=uuid4(),
            correlation_id=cid,  # type: ignore[arg-type]
            lifecycle_type=EnumAgentTaskLifecycleType.COMPLETED,
            remote_task_handle="task-abc",
            occurred_at=datetime.now(UTC),
        )
        return _make_envelope(lifecycle_event, cid)

    async def test_publish_envelope_called_on_lifecycle_completed(self) -> None:
        bus = _make_mock_bus()
        handler = HandlerDelegationWorkflow()
        dispatcher = DispatcherAgentTaskLifecycle(handler, event_bus=bus)  # type: ignore[arg-type]

        cid = uuid4()
        _start_agent_invocation(handler, cid)

        envelope = self._make_completed_lifecycle_envelope(cid)
        result = await dispatcher.handle(envelope)

        assert result.status == EnumDispatchStatus.SUCCESS
        assert bus.publish_envelope.called

        published_topics = {
            c.kwargs["topic"] for c in bus.publish_envelope.call_args_list
        }
        assert (
            TOPIC_DELEGATION_COMPLETED in published_topics
            or TOPIC_DELEGATION_FAILED in published_topics
        )

    async def test_output_events_empty_when_bus_wired(self) -> None:
        """Bus-wired path: routable events published directly; topicless events may remain."""
        bus = _make_mock_bus()
        handler = HandlerDelegationWorkflow()
        dispatcher = DispatcherAgentTaskLifecycle(handler, event_bus=bus)  # type: ignore[arg-type]

        cid = uuid4()
        _start_agent_invocation(handler, cid)

        envelope = self._make_completed_lifecycle_envelope(cid)
        result = await dispatcher.handle(envelope)

        assert result.status == EnumDispatchStatus.SUCCESS
        # All events with a .topic attribute must have been published directly.
        for event in result.output_events:
            assert not hasattr(event, "topic") or event.topic is None  # type: ignore[union-attr]
