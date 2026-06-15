# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerSwarmDispatchOrchestrator FSM state transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.nodes.node_swarm_dispatch_orchestrator.handlers.handler_swarm_dispatch import (
    HandlerSwarmDispatchOrchestrator,
    InvalidFSMTransitionError,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.enums import (
    EnumSwarmOrchestratorState,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_orchestrator_state import (
    ModelOrchestratorState,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_swarm_dispatch_request import (
    ModelSwarmDispatchRequest,
)

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )


@pytest.fixture
def handler() -> HandlerSwarmDispatchOrchestrator:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

    mock_bus = MagicMock(spec=ProtocolEventBusPublisher)
    mock_bus.publish = AsyncMock()
    return HandlerSwarmDispatchOrchestrator(
        event_bus=cast("ProtocolEventBusPublisher", mock_bus)
    )


@pytest.fixture
def request_fixture() -> ModelSwarmDispatchRequest:
    return ModelSwarmDispatchRequest(
        task="Build a REST API",
        endpoint_ids=("ep-1", "ep-2"),
        run_id="run-001",
        correlation_id="corr-001",
    )


@pytest.fixture
def received_state(
    request_fixture: ModelSwarmDispatchRequest,
) -> ModelOrchestratorState:
    return ModelOrchestratorState(
        fsm_state=EnumSwarmOrchestratorState.RECEIVED,
        run_id=request_fixture.run_id,
        correlation_id=request_fixture.correlation_id,
        original_task=request_fixture.task,
    )


_HEALTH_EVENT = {
    "endpoint_health": {
        "ep-1": {"endpoint_status": "reachable", "latency_ms": 42},
        "ep-2": {"endpoint_status": "reachable", "latency_ms": 55},
    }
}

_DECOMPOSE_EVENT = {
    "subtasks": [
        {
            "subtask_id": "st-1",
            "description": "Define routes",
            "model_affinity": "",
            "depends_on": [],
            "estimated_tokens": 100,
            "category": "code",
        },
        {
            "subtask_id": "st-2",
            "description": "Write handlers",
            "model_affinity": "",
            "depends_on": ["st-1"],
            "estimated_tokens": 200,
            "category": "code",
        },
    ]
}

_SELECT_EVENT = {"assignments": {"st-1": "ep-1", "st-2": "ep-2"}}

_FANOUT_EVENT = {
    "dispatches": [
        {
            "subtask_id": "st-1",
            "endpoint_id": "ep-1",
            "status": "succeeded",
            "latency_ms": 800,
            "result_text": "routes done",
            "failure_reason": "",
            "wave": 0,
            "model_id": "qwen3",
            "base_url": "https://endpoint-a.example.invalid",
        },
        {
            "subtask_id": "st-2",
            "endpoint_id": "ep-2",
            "status": "succeeded",
            "latency_ms": 1200,
            "result_text": "handlers done",
            "failure_reason": "",
            "wave": 1,
            "model_id": "deepseek",
            "base_url": "https://endpoint-b.example.invalid",
        },
    ]
}

_AGGREGATE_EVENT = {
    "aggregated_output": "## Subtask: st-1\nroutes done\n\n## Subtask: st-2\nhandlers done"
}


class TestFSMHappyPath:
    def test_transition_received_returns_received_state(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
        request_fixture: ModelSwarmDispatchRequest,
    ) -> None:
        state, publishes = handler.transition_received(received_state, request_fixture)
        assert state.fsm_state == EnumSwarmOrchestratorState.RECEIVED
        assert len(publishes) == 1

    def test_transition_health_checked(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        state, _ = handler.transition_health_checked(received_state, _HEALTH_EVENT)
        assert state.fsm_state == EnumSwarmOrchestratorState.HEALTH_CHECKED
        assert "ep-1" in state.endpoint_health
        assert state.endpoint_health["ep-1"].status == "reachable"
        assert state.endpoint_health["ep-1"].latency_ms == 42

    def test_transition_decomposed(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        health_state, _ = handler.transition_health_checked(
            received_state, _HEALTH_EVENT
        )
        state, _ = handler.transition_decomposed(health_state, _DECOMPOSE_EVENT)
        assert state.fsm_state == EnumSwarmOrchestratorState.DECOMPOSED
        assert len(state.subtasks) == 2
        assert state.subtasks[0].subtask_id == "st-1"

    def test_transition_endpoints_selected(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        health_state, _ = handler.transition_health_checked(
            received_state, _HEALTH_EVENT
        )
        decomposed_state, _ = handler.transition_decomposed(
            health_state, _DECOMPOSE_EVENT
        )
        state, _ = handler.transition_endpoints_selected(
            decomposed_state, _SELECT_EVENT
        )
        assert state.fsm_state == EnumSwarmOrchestratorState.ENDPOINTS_SELECTED
        assert state.assignments == {"st-1": "ep-1", "st-2": "ep-2"}

    def test_transition_dispatching(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        health_state, _ = handler.transition_health_checked(
            received_state, _HEALTH_EVENT
        )
        decomposed_state, _ = handler.transition_decomposed(
            health_state, _DECOMPOSE_EVENT
        )
        selected_state, _ = handler.transition_endpoints_selected(
            decomposed_state, _SELECT_EVENT
        )
        state, _ = handler.transition_dispatching(selected_state, _FANOUT_EVENT)
        assert state.fsm_state == EnumSwarmOrchestratorState.DISPATCHING
        assert len(state.dispatches) == 2

    def test_transition_aggregating(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        health_state, _ = handler.transition_health_checked(
            received_state, _HEALTH_EVENT
        )
        decomposed_state, _ = handler.transition_decomposed(
            health_state, _DECOMPOSE_EVENT
        )
        selected_state, _ = handler.transition_endpoints_selected(
            decomposed_state, _SELECT_EVENT
        )
        dispatching_state, _ = handler.transition_dispatching(
            selected_state, _FANOUT_EVENT
        )
        state, _ = handler.transition_aggregating(
            dispatching_state, _AGGREGATE_EVENT, total_latency_ms=2500
        )
        assert state.fsm_state == EnumSwarmOrchestratorState.AGGREGATING
        assert "routes done" in state.aggregated_output
        assert state.total_latency_ms == 2500

    def test_transition_completed(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        health_state, _ = handler.transition_health_checked(
            received_state, _HEALTH_EVENT
        )
        decomposed_state, _ = handler.transition_decomposed(
            health_state, _DECOMPOSE_EVENT
        )
        selected_state, _ = handler.transition_endpoints_selected(
            decomposed_state, _SELECT_EVENT
        )
        dispatching_state, _ = handler.transition_dispatching(
            selected_state, _FANOUT_EVENT
        )
        aggregating_state, _ = handler.transition_aggregating(
            dispatching_state, _AGGREGATE_EVENT
        )
        state, publishes = handler.transition_completed(aggregating_state)
        assert state.fsm_state == EnumSwarmOrchestratorState.COMPLETED
        assert len(publishes) == 1

    def test_full_chain_via_handle(
        self,
        request_fixture: ModelSwarmDispatchRequest,
    ) -> None:
        # Sync path: no event_bus injected → handle() returns ModelSwarmDispatchResult
        sync_handler = HandlerSwarmDispatchOrchestrator()
        result = sync_handler.handle(request_fixture)
        assert result is not None
        assert result.run_id == "run-001"
        assert result.correlation_id == "corr-001"


class TestFSMFailurePaths:
    def test_failure_from_received(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        state, publishes = handler.transition_failed(
            received_state, "health probe timeout"
        )
        assert state.fsm_state == EnumSwarmOrchestratorState.FAILED
        assert state.error == "health probe timeout"
        assert len(publishes) == 1

    def test_failure_from_health_checked(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        health_state, _ = handler.transition_health_checked(
            received_state, _HEALTH_EVENT
        )
        state, _ = handler.transition_failed(health_state, "decompose timeout")
        assert state.fsm_state == EnumSwarmOrchestratorState.FAILED

    def test_failure_from_decomposed(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        health_state, _ = handler.transition_health_checked(
            received_state, _HEALTH_EVENT
        )
        decomposed_state, _ = handler.transition_decomposed(
            health_state, _DECOMPOSE_EVENT
        )
        state, _ = handler.transition_failed(
            decomposed_state, "endpoint selection failed"
        )
        assert state.fsm_state == EnumSwarmOrchestratorState.FAILED

    def test_failure_from_dispatching(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        health_state, _ = handler.transition_health_checked(
            received_state, _HEALTH_EVENT
        )
        decomposed_state, _ = handler.transition_decomposed(
            health_state, _DECOMPOSE_EVENT
        )
        selected_state, _ = handler.transition_endpoints_selected(
            decomposed_state, _SELECT_EVENT
        )
        dispatching_state, _ = handler.transition_dispatching(
            selected_state, _FANOUT_EVENT
        )
        state, _ = handler.transition_failed(dispatching_state, "aggregation failed")
        assert state.fsm_state == EnumSwarmOrchestratorState.FAILED


class TestFSMInvalidTransitions:
    def test_invalid_health_check_from_decomposed_state(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        health_state, _ = handler.transition_health_checked(
            received_state, _HEALTH_EVENT
        )
        decomposed_state, _ = handler.transition_decomposed(
            health_state, _DECOMPOSE_EVENT
        )
        with pytest.raises(InvalidFSMTransitionError):
            handler.transition_health_checked(decomposed_state, _HEALTH_EVENT)

    def test_invalid_decompose_from_received(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        with pytest.raises(InvalidFSMTransitionError):
            handler.transition_decomposed(received_state, _DECOMPOSE_EVENT)

    def test_invalid_endpoints_selected_from_received(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        with pytest.raises(InvalidFSMTransitionError):
            handler.transition_endpoints_selected(received_state, _SELECT_EVENT)

    def test_invalid_completed_from_dispatching(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        received_state: ModelOrchestratorState,
    ) -> None:
        health_state, _ = handler.transition_health_checked(
            received_state, _HEALTH_EVENT
        )
        decomposed_state, _ = handler.transition_decomposed(
            health_state, _DECOMPOSE_EVENT
        )
        selected_state, _ = handler.transition_endpoints_selected(
            decomposed_state, _SELECT_EVENT
        )
        dispatching_state, _ = handler.transition_dispatching(
            selected_state, _FANOUT_EVENT
        )
        with pytest.raises(InvalidFSMTransitionError):
            handler.transition_completed(dispatching_state)
