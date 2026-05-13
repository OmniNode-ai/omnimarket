# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for delegation dispatcher and route wiring.

Verifies that wire_delegation_dispatchers() registers both dispatchers
and routes with the MessageDispatchEngine.

Related:
    - OMN-7040: Node-based delegation pipeline
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.nodes.node_delegation_orchestrator.wiring import (
    ROUTE_ID_AGENT_TASK_LIFECYCLE,
    ROUTE_ID_DELEGATION_REQUEST,
    ROUTE_ID_INVOCATION_COMMAND,
    ROUTE_ID_QUALITY_GATE_RESULT,
    ROUTE_ID_ROUTING_DECISION,
    wire_delegation_dispatchers,
)


@pytest.fixture
def mock_container() -> MagicMock:
    """Create a mock container with a service registry that resolves HandlerDelegationWorkflow."""
    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        HandlerDelegationWorkflow,
    )

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
