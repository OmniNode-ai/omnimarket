# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for delegation dispatcher and route wiring.

Verifies that wire_delegation_dispatchers() registers both dispatchers
and routes with the MessageDispatchEngine.

Related:
    - OMN-7040: Node-based delegation pipeline
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import NAMESPACE_DNS, uuid4, uuid5

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumDispatchStatus

from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_delegation_request import (
    DispatcherDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_routing_decision import (
    DispatcherRoutingDecision,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.wiring import (
    ROUTE_ID_AGENT_TASK_LIFECYCLE,
    ROUTE_ID_DELEGATION_REQUEST,
    ROUTE_ID_INVOCATION_COMMAND,
    ROUTE_ID_QUALITY_GATE_RESULT,
    ROUTE_ID_ROUTING_DECISION,
    get_shared_delegation_workflow_handler,
    wire_delegation_dispatchers,
    wire_delegation_handlers,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)


def _make_envelope(
    payload: object, correlation_id: object
) -> ModelEventEnvelope[object]:
    return ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=payload,
        correlation_id=correlation_id,  # type: ignore[arg-type]
        envelope_timestamp=datetime.now(UTC),
    )


def _make_request(correlation_id: object) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Run ticket-pipeline for OMN-11076",
        task_type="research",
        correlation_id=correlation_id,  # type: ignore[arg-type]
        max_tokens=1024,
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(correlation_id: object) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,  # type: ignore[arg-type]
        task_type="research",
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(
            NAMESPACE_DNS, "omninode.ai/backends/qwen3-coder-30b"
        ),
        endpoint_url="https://delegation-llm.test:8000",
        cost_tier="low",
        max_context_tokens=65536,
        system_prompt="You are a delegation test assistant.",
        rationale="Routing test.",
    )


@pytest.fixture
def mock_container() -> MagicMock:
    """Create a mock container with a service registry that resolves HandlerDelegationWorkflow."""
    container = MagicMock()
    handler = HandlerDelegationWorkflow()
    container.service_registry.resolve_service = AsyncMock(return_value=handler)
    return container


@pytest.fixture
def mock_engine() -> MagicMock:
    """Create a mock MessageDispatchEngine."""
    engine = MagicMock()
    engine.register_dispatcher = MagicMock()
    engine.register_route = MagicMock()
    return engine


@pytest.mark.unit
class TestWireDelegationDispatchers:
    """wire_delegation_dispatchers must register all delegation routes."""

    @pytest.mark.asyncio
    async def test_registers_dispatchers(
        self, mock_container: MagicMock, mock_engine: MagicMock
    ) -> None:
        result = await wire_delegation_dispatchers(mock_container, mock_engine)

        assert len(result["dispatchers"]) == 5
        assert mock_engine.register_dispatcher.call_count == 5

    @pytest.mark.asyncio
    async def test_registers_routes(
        self, mock_container: MagicMock, mock_engine: MagicMock
    ) -> None:
        result = await wire_delegation_dispatchers(mock_container, mock_engine)

        assert len(result["routes"]) == 5
        assert mock_engine.register_route.call_count == 5

    @pytest.mark.asyncio
    async def test_route_ids_are_correct(
        self, mock_container: MagicMock, mock_engine: MagicMock
    ) -> None:
        result = await wire_delegation_dispatchers(mock_container, mock_engine)

        assert ROUTE_ID_DELEGATION_REQUEST in result["routes"]
        assert ROUTE_ID_INVOCATION_COMMAND in result["routes"]
        assert ROUTE_ID_ROUTING_DECISION in result["routes"]
        assert ROUTE_ID_QUALITY_GATE_RESULT in result["routes"]
        assert ROUTE_ID_AGENT_TASK_LIFECYCLE in result["routes"]

    @pytest.mark.asyncio
    async def test_dispatcher_ids_are_correct(
        self, mock_container: MagicMock, mock_engine: MagicMock
    ) -> None:
        result = await wire_delegation_dispatchers(mock_container, mock_engine)

        assert "dispatcher.delegation.request" in result["dispatchers"]
        assert "dispatcher.delegation.invocation" in result["dispatchers"]
        assert "dispatcher.delegation.routing-decision" in result["dispatchers"]
        assert "dispatcher.delegation.quality-gate-result" in result["dispatchers"]
        assert "dispatcher.delegation.agent-task-lifecycle" in result["dispatchers"]

    @pytest.mark.asyncio
    async def test_status_is_success(
        self, mock_container: MagicMock, mock_engine: MagicMock
    ) -> None:
        result = await wire_delegation_dispatchers(mock_container, mock_engine)

        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_routes_have_correct_model_types(
        self, mock_container: MagicMock, mock_engine: MagicMock
    ) -> None:
        from omnibase_infra.models.dispatch.model_dispatch_route import (
            ModelDispatchRoute,
        )

        await wire_delegation_dispatchers(mock_container, mock_engine)

        for call in mock_engine.register_route.call_args_list:
            route = call[0][0]
            assert isinstance(route, ModelDispatchRoute)

    @pytest.mark.asyncio
    async def test_wired_dispatchers_share_one_workflow_handler(
        self, mock_container: MagicMock, mock_engine: MagicMock
    ) -> None:
        await wire_delegation_dispatchers(mock_container, mock_engine)

        dispatcher_handlers = [
            call.kwargs["dispatcher"].__self__._handler
            for call in mock_engine.register_dispatcher.call_args_list
        ]

        assert len(dispatcher_handlers) == 5
        assert len({id(handler) for handler in dispatcher_handlers}) == 1

    @pytest.mark.asyncio
    async def test_wire_handlers_reuses_process_shared_workflow_handler(self) -> None:
        container = MagicMock()
        container.service_registry.register_instance = AsyncMock()

        await wire_delegation_handlers(container)
        await wire_delegation_handlers(container)

        registered = [
            call.kwargs["instance"]
            for call in container.service_registry.register_instance.call_args_list
        ]
        assert registered == [
            get_shared_delegation_workflow_handler(),
            get_shared_delegation_workflow_handler(),
        ]

    @pytest.mark.asyncio
    async def test_split_dispatcher_handler_instances_emit_inference_after_routing(
        self,
    ) -> None:
        request_handler = HandlerDelegationWorkflow()
        routing_handler = HandlerDelegationWorkflow()
        request_dispatcher = DispatcherDelegationRequest(request_handler)
        routing_dispatcher = DispatcherRoutingDecision(routing_handler)
        cid = uuid4()

        request_result = await request_dispatcher.handle(
            _make_envelope(_make_request(cid), cid)
        )
        routing_result = await routing_dispatcher.handle(
            _make_envelope(_make_routing_decision(cid), cid)
        )

        assert request_handler is not routing_handler
        assert request_result.status == EnumDispatchStatus.SUCCESS
        assert routing_result.status == EnumDispatchStatus.SUCCESS
        assert len(request_result.output_events) == 1
        assert isinstance(request_result.output_events[0], ModelRoutingIntent)
        assert len(routing_result.output_events) == 1
        assert isinstance(routing_result.output_events[0], ModelInferenceIntent)
        assert routing_result.output_events[0].correlation_id == cid
