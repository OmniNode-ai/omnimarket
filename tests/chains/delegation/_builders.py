# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs and a driver for the delegation-orchestrator chain cases (OMN-19713).

Every case drives the REAL ``HandlerDelegationWorkflow``. Nothing it calls is
replaced by a double. Only deployment facts are pinned, by the autouse fixture
in this package's ``conftest.py``: the bifrost routing contract. That pin exists
because the handler otherwise reads a host overlay under the user's home, so
the same chain would take a different path on a laptop and on a runner.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

from omnibase_core.enums import EnumInvocationKind
from omnibase_core.enums.enum_agent_protocol import EnumAgentProtocol
from omnibase_core.enums.enum_agent_task_lifecycle_type import (
    EnumAgentTaskLifecycleType,
)
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.delegation.model_agent_task_lifecycle_event import (
    ModelAgentTaskLifecycleEvent,
)
from omnibase_core.models.delegation.model_invocation_command import (
    ModelInvocationCommand,
)
from omnibase_infra.runtime.boundary_failure_terminal import (
    ModelBoundaryFailureTerminal,
)
from pydantic import BaseModel

from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
    TOPIC_ID_DELEGATION_COMPLETED_V2,
    TOPIC_ID_DELEGATION_FAILED,
    TOPIC_ID_DELEGATION_FAILED_ROUTED_V2,
    TOPIC_ID_DELEGATION_FAILED_UNROUTED_V2,
    TOPIC_ID_INFERENCE_REQUEST,
    TOPIC_ID_INVOCATION_COMMAND,
    TOPIC_ID_QUALITY_GATE_REQUEST,
    TOPIC_ID_ROUTING_REQUEST,
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
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from tests.chains.chain_assert import ChainEvent, ChainRecorder

TENANT = "omn19713-tenant"
MODEL = "qwen3-coder-30b"
ENDPOINT = "http://delegation-llm.test:8000"
BACKEND_REF = "local-qwen3-coder-30b"

# Contract topics for every class these chains emit. A class with no dedicated
# binding in contract_topics (the typed escalation proof) is published on its
# class name, and says so here rather than guessing a topic.
_TOPIC_BY_EVENT_TYPE: dict[str, str] = {
    "ModelRoutingIntent": TOPIC_ID_ROUTING_REQUEST,
    "ModelInferenceIntent": TOPIC_ID_INFERENCE_REQUEST,
    "ModelQualityGateIntent": TOPIC_ID_QUALITY_GATE_REQUEST,
    "ModelInvocationCommand": TOPIC_ID_INVOCATION_COMMAND,
    "ModelDelegationCompleted": TOPIC_ID_DELEGATION_COMPLETED,
    "ModelDelegationTerminalCompletedV2": TOPIC_ID_DELEGATION_COMPLETED_V2,
    "ModelDelegationFailed": TOPIC_ID_DELEGATION_FAILED,
    "ModelDelegationTerminalFailedRoutedV2": TOPIC_ID_DELEGATION_FAILED_ROUTED_V2,
    "ModelDelegationTerminalFailedUnroutedV2": TOPIC_ID_DELEGATION_FAILED_UNROUTED_V2,
}


def request(cid: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
        tenant_id=TENANT,
    )


def routing_decision(
    cid: UUID, *, tier_name: str, backend_ref: str = BACKEND_REF
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="test",
        selected_model=MODEL,
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/local-qwen"),
        endpoint_url=ENDPOINT,
        cost_tier="low",
        tier_name=tier_name,
        max_context_tokens=65536,
        max_tokens=4096,
        system_prompt="You are a test generation assistant.",
        rationale=f"Task 'test' routed via tier '{tier_name}'.",
        selected_backend_ref=backend_ref,
    )


def inference_ok(cid: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content="def test_verify_registration():\n    assert True",
        model_used=MODEL,
        llm_call_id="chatcmpl-omn19713",
        latency_ms=1200,
        prompt_tokens=1234,
        completion_tokens=567,
        total_tokens=1801,
    )


def inference_error(cid: UUID, message: str) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content="",
        model_used=MODEL,
        llm_call_id="chatcmpl-omn19713-error",
        latency_ms=10,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        error_message=message,
    )


def gate_result(cid: UUID, *, passed: bool) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=cid,
        passed=passed,
        quality_score=0.95 if passed else 0.2,
        failure_reasons=() if passed else ("refusal_detected",),
        fallback_recommended=not passed,
    )


def invocation_command(cid: UUID) -> ModelInvocationCommand:
    return ModelInvocationCommand(
        task_id=uuid4(),
        correlation_id=cid,
        target_ref="agent://remote",
        invocation_kind=EnumInvocationKind.AGENT,
        agent_protocol=EnumAgentProtocol.A2A,
    )


def lifecycle(
    cid: UUID, kind: EnumAgentTaskLifecycleType, *, error: str | None = None
) -> ModelAgentTaskLifecycleEvent:
    return ModelAgentTaskLifecycleEvent(
        task_id=uuid4(),
        correlation_id=cid,
        lifecycle_type=kind,
        remote_task_handle="remote-omn19713",
        occurred_at=datetime.now(UTC),
        error=error,
    )


def boundary_failure(cid: UUID, *, origin_topic: str) -> ModelBoundaryFailureTerminal:
    return ModelBoundaryFailureTerminal(
        correlation_id=cid,
        origin_topic=origin_topic,
        failure_class="ProtocolConfigurationError",
        failure_code="ONEX_CORE_041_INVALID_CONFIGURATION",
        failure_reason="leg configuration did not resolve",
        retryable=False,
    )


Step = Callable[[HandlerDelegationWorkflow, UUID], Sequence[BaseModel]]


@dataclass(frozen=True)
class ChainRun:
    events: tuple[ChainEvent, ...]
    states: tuple[EnumDelegationState, ...]
    escalation_count: int
    bus_history_count: int


async def drive(steps: Sequence[Step]) -> tuple[UUID, ChainRun]:
    """Drive ``steps`` through a fresh real handler; publish every emitted event.

    ``states`` is the workflow state observed after each step. With the event
    list it pins which orchestrator path ran, since the handler does not record
    the transitions it fired.
    """
    cid = uuid4()
    handler = HandlerDelegationWorkflow()
    recorder = ChainRecorder(EventBusInmemory(environment="test", group=str(cid)))
    states: list[EnumDelegationState] = []
    for step in steps:
        for event in step(handler, cid):
            await recorder.publish(
                _TOPIC_BY_EVENT_TYPE.get(type(event).__name__, type(event).__name__),
                event,
                correlation_id=cid,
                causation_id=cid if recorder.events else None,
            )
        states.append(handler.workflows[cid].state)
    workflow = handler.workflows[cid]
    return cid, ChainRun(
        events=recorder.events,
        states=tuple(states),
        escalation_count=workflow.escalation_count,
        bus_history_count=await recorder.bus_history_count(),
    )
