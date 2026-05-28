# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation domain wiring for MessageDispatchEngine integration.

Registers delegation handlers in the DI container and wires dispatchers
into the MessageDispatchEngine for event-driven routing.

The intermediate intent topics (routing-request, inference-request,
quality-gate-request) are consumed natively by their owning worker nodes
(routing reducer, LLM call effect, quality gate reducer) as bus consumers;
there is no in-process bridge.

Related:
    - OMN-7040: Node-based delegation pipeline
    - OMN-12294: Pure Kafka delegation chain (in-process intent bridge removed)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID

from omnibase_core.enums import EnumInjectionScope, EnumMessageCategory

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer
    from omnibase_core.protocols.event_bus.protocol_event_bus import ProtocolEventBus
    from omnibase_infra.runtime import MessageDispatchEngine

    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        HandlerDelegationWorkflow,
    )

logger = logging.getLogger(__name__)

# Route IDs for delegation dispatchers
ROUTE_ID_DELEGATION_REQUEST = "route.delegation.delegation-request"
ROUTE_ID_INVOCATION_COMMAND = "route.delegation.invocation"
ROUTE_ID_ROUTING_DECISION = "route.delegation.routing-decision"
ROUTE_ID_INFERENCE_RESPONSE = "route.delegation.inference-response"
ROUTE_ID_QUALITY_GATE_RESULT = "route.delegation.quality-gate-result"
ROUTE_ID_AGENT_TASK_LIFECYCLE = "route.delegation.agent-task-lifecycle"

_SHARED_WORKFLOW_HANDLER: HandlerDelegationWorkflow | None = None


class WiringResult(TypedDict):
    services: list[str]
    status: str


def get_shared_delegation_workflow_handler() -> HandlerDelegationWorkflow:
    """Return the process-local delegation workflow FSM used by live dispatchers."""
    global _SHARED_WORKFLOW_HANDLER

    if _SHARED_WORKFLOW_HANDLER is None:
        from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
            HandlerDelegationWorkflow,
        )

        _SHARED_WORKFLOW_HANDLER = HandlerDelegationWorkflow()
    return _SHARED_WORKFLOW_HANDLER


async def wire_delegation_handlers(
    container: ModelONEXContainer,
) -> WiringResult:
    """Register delegation handlers with the container.

    Registers:
    - HandlerDelegationWorkflow (orchestrator FSM)

    Args:
        container: ONEX container instance to register services in.

    Returns:
        WiringResult with list of registered service names.
    """
    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        HandlerDelegationWorkflow,
    )

    services_registered: list[str] = []

    # HandlerDelegationWorkflow — stateful orchestrator FSM
    workflow_handler = get_shared_delegation_workflow_handler()
    if container.service_registry is not None:
        await container.service_registry.register_instance(
            interface=HandlerDelegationWorkflow,
            instance=workflow_handler,
            scope=EnumInjectionScope.GLOBAL,
            metadata={
                "description": "Delegation workflow orchestrator (FSM)",
            },
        )
    services_registered.append("HandlerDelegationWorkflow")
    logger.debug("Registered HandlerDelegationWorkflow in container")

    return WiringResult(services=services_registered, status="success")


async def wire_delegation_dispatchers(
    container: ModelONEXContainer,
    engine: MessageDispatchEngine,
    correlation_id: UUID | None = None,
    event_bus: ProtocolEventBus | None = None,
) -> dict[str, list[str] | str]:
    """Wire delegation dispatchers into MessageDispatchEngine.

    Creates dispatcher adapters for the delegation handler and registers
    them with the MessageDispatchEngine.

    Args:
        container: ONEX container with registered handlers.
        engine: MessageDispatchEngine to register dispatchers with.
        correlation_id: Optional correlation ID for error tracking.
        event_bus: Optional event bus for output event publishing.

    Returns:
        Summary dict with dispatchers, routes, and status.
    """
    from omnibase_infra.models.dispatch.model_dispatch_route import ModelDispatchRoute

    from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_agent_task_lifecycle import (
        DispatcherAgentTaskLifecycle,
    )
    from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_delegation_request import (
        DispatcherDelegationRequest,
    )
    from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_inference_response import (
        DispatcherInferenceResponse,
    )
    from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_invocation_command import (
        DispatcherInvocationCommand,
    )
    from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_quality_gate_result import (
        DispatcherQualityGateResult,
    )
    from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_routing_decision import (
        DispatcherRoutingDecision,
    )
    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        HandlerDelegationWorkflow,
    )

    dispatchers_registered: list[str] = []
    routes_registered: list[str] = []

    # Resolve the workflow handler from the container
    handler: HandlerDelegationWorkflow = (
        await container.service_registry.resolve_service(HandlerDelegationWorkflow)
    )

    # 1. DispatcherDelegationRequest — handles incoming delegation commands
    dispatcher_request = DispatcherDelegationRequest(handler, event_bus=event_bus)
    engine.register_dispatcher(
        dispatcher_id=dispatcher_request.dispatcher_id,
        dispatcher=dispatcher_request.handle,
        category=dispatcher_request.category,
        message_types=dispatcher_request.message_types,
    )
    dispatchers_registered.append(dispatcher_request.dispatcher_id)

    route_delegation_request = ModelDispatchRoute(
        route_id=ROUTE_ID_DELEGATION_REQUEST,
        topic_pattern="*.cmd.*.delegation-request.*",
        message_category=EnumMessageCategory.COMMAND,
        dispatcher_id=dispatcher_request.dispatcher_id,
        message_type="omnibase-infra.delegation-request",
    )
    engine.register_route(route_delegation_request)
    routes_registered.append(route_delegation_request.route_id)

    # 2. DispatcherInvocationCommand — handles reducer output for A2A dispatch
    dispatcher_invocation = DispatcherInvocationCommand(handler, event_bus=event_bus)
    engine.register_dispatcher(
        dispatcher_id=dispatcher_invocation.dispatcher_id,
        dispatcher=dispatcher_invocation.handle,
        category=dispatcher_invocation.category,
        message_types=dispatcher_invocation.message_types,
    )
    dispatchers_registered.append(dispatcher_invocation.dispatcher_id)

    route_invocation_command = ModelDispatchRoute(
        route_id=ROUTE_ID_INVOCATION_COMMAND,
        topic_pattern="*.cmd.*.invocation.*",
        message_category=EnumMessageCategory.COMMAND,
        dispatcher_id=dispatcher_invocation.dispatcher_id,
        message_type="omnibase-infra.invocation",
    )
    engine.register_route(route_invocation_command)
    routes_registered.append(route_invocation_command.route_id)

    # 3. DispatcherRoutingDecision — handles routing decisions from reducer
    dispatcher_routing = DispatcherRoutingDecision(handler, event_bus=event_bus)
    engine.register_dispatcher(
        dispatcher_id=dispatcher_routing.dispatcher_id,
        dispatcher=dispatcher_routing.handle,
        category=dispatcher_routing.category,
        message_types=dispatcher_routing.message_types,
    )
    dispatchers_registered.append(dispatcher_routing.dispatcher_id)

    route_routing_decision = ModelDispatchRoute(
        route_id=ROUTE_ID_ROUTING_DECISION,
        topic_pattern="*.evt.*.routing-decision.*",
        message_category=EnumMessageCategory.EVENT,
        dispatcher_id=dispatcher_routing.dispatcher_id,
        message_type="omnibase-infra.routing-decision",
    )
    engine.register_route(route_routing_decision)
    routes_registered.append(route_routing_decision.route_id)

    # 4. DispatcherInferenceResponse — handles inference responses from the LLM call effect
    dispatcher_inference = DispatcherInferenceResponse(handler, event_bus=event_bus)
    engine.register_dispatcher(
        dispatcher_id=dispatcher_inference.dispatcher_id,
        dispatcher=dispatcher_inference.handle,
        category=dispatcher_inference.category,
        message_types=dispatcher_inference.message_types,
    )
    dispatchers_registered.append(dispatcher_inference.dispatcher_id)

    route_inference_response = ModelDispatchRoute(
        route_id=ROUTE_ID_INFERENCE_RESPONSE,
        topic_pattern="*.evt.*.inference-response.*",
        message_category=EnumMessageCategory.EVENT,
        dispatcher_id=dispatcher_inference.dispatcher_id,
        message_type="omnibase-infra.inference-response",
    )
    engine.register_route(route_inference_response)
    routes_registered.append(route_inference_response.route_id)

    # 5. DispatcherQualityGateResult — handles quality gate results
    dispatcher_gate = DispatcherQualityGateResult(handler, event_bus=event_bus)
    engine.register_dispatcher(
        dispatcher_id=dispatcher_gate.dispatcher_id,
        dispatcher=dispatcher_gate.handle,
        category=dispatcher_gate.category,
        message_types=dispatcher_gate.message_types,
    )
    dispatchers_registered.append(dispatcher_gate.dispatcher_id)

    route_quality_gate = ModelDispatchRoute(
        route_id=ROUTE_ID_QUALITY_GATE_RESULT,
        topic_pattern="*.evt.*.quality-gate-result.*",
        message_category=EnumMessageCategory.EVENT,
        dispatcher_id=dispatcher_gate.dispatcher_id,
        message_type="omnibase-infra.quality-gate-result",
    )
    engine.register_route(route_quality_gate)
    routes_registered.append(route_quality_gate.route_id)

    # 6. DispatcherAgentTaskLifecycle — handles A2A lifecycle events
    dispatcher_lifecycle = DispatcherAgentTaskLifecycle(handler, event_bus=event_bus)
    engine.register_dispatcher(
        dispatcher_id=dispatcher_lifecycle.dispatcher_id,
        dispatcher=dispatcher_lifecycle.handle,
        category=dispatcher_lifecycle.category,
        message_types=dispatcher_lifecycle.message_types,
    )
    dispatchers_registered.append(dispatcher_lifecycle.dispatcher_id)

    route_agent_task_lifecycle = ModelDispatchRoute(
        route_id=ROUTE_ID_AGENT_TASK_LIFECYCLE,
        topic_pattern="*.evt.*.agent-task-lifecycle.*",
        message_category=EnumMessageCategory.EVENT,
        dispatcher_id=dispatcher_lifecycle.dispatcher_id,
        message_type="omnibase-infra.agent-task-lifecycle",
    )
    engine.register_route(route_agent_task_lifecycle)
    routes_registered.append(route_agent_task_lifecycle.route_id)

    logger.info(
        "Delegation dispatchers wired: %s (correlation_id=%s)",
        dispatchers_registered,
        correlation_id,
    )

    return {
        "dispatchers": dispatchers_registered,
        "routes": routes_registered,
        "status": "success",
    }
