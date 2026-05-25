# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Integration test: FSM state routing and persistence for swarm dispatch orchestrator.

Proves the full FSM traversal:
  RECEIVED → HEALTH_CHECKED → DECOMPOSED → ENDPOINTS_SELECTED →
  DISPATCHING → AGGREGATING → COMPLETED

All state transitions happen via ``route_event`` using an in-memory
ProtocolStateStore stub, verifying that:
  - Each response topic advances the FSM to the correct state
  - FSM state is persisted after every transition
  - Final state is COMPLETED with correct aggregated output
  - MissingRunStateError is raised for unknown run_id
  - ValueError is raised for unmapped topics

OMN-12003
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope

from omnimarket.nodes.node_swarm_dispatch_orchestrator.handlers.handler_swarm_dispatch import (
    HandlerSwarmDispatchOrchestrator,
    MissingRunStateError,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.enums import (
    EnumSwarmOrchestratorState,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_swarm_dispatch_request import (
    ModelSwarmDispatchRequest,
)

# ---------------------------------------------------------------------------
# In-memory ProtocolStateStore stub
# ---------------------------------------------------------------------------


class _MemoryStateStore:
    """Minimal in-memory implementation of ProtocolStateStore for tests."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], ModelStateEnvelope] = {}

    async def get(
        self, node_id: str, scope_id: str = "default"
    ) -> ModelStateEnvelope | None:
        return self._store.get((node_id, scope_id))

    async def put(self, envelope: ModelStateEnvelope) -> None:
        self._store[(envelope.node_id, envelope.scope_id)] = envelope

    async def delete(self, node_id: str, scope_id: str = "default") -> bool:
        key = (node_id, scope_id)
        if key in self._store:
            del self._store[key]
            return True
        return False

    async def exists(self, node_id: str, scope_id: str = "default") -> bool:
        return (node_id, scope_id) in self._store

    async def list_keys(self, node_id: str | None = None) -> list[tuple[str, str]]:
        return sorted(k for k in self._store if node_id is None or k[0] == node_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_RUN_ID = "run-integration-001"
_CORR_ID = "corr-integration-001"

_HEALTH_COMPLETED_TOPIC = "onex.evt.omnimarket.swarm-endpoint-health-completed.v1"  # onex-topic-allow: test fixture constant mirrors contract.yaml subscribe_topics
_DECOMPOSITION_COMPLETED_TOPIC = "onex.evt.omnimarket.swarm-decomposition-completed.v1"  # onex-topic-allow: test fixture constant mirrors contract.yaml subscribe_topics
_ENDPOINTS_SELECTED_TOPIC = "onex.evt.omnimarket.swarm-endpoints-selected.v1"  # onex-topic-allow: test fixture constant mirrors contract.yaml subscribe_topics
_FANOUT_COMPLETED_TOPIC = "onex.evt.omnimarket.swarm-fanout-completed.v1"  # onex-topic-allow: test fixture constant mirrors contract.yaml subscribe_topics
_AGGREGATION_COMPLETED_TOPIC = "onex.evt.omnimarket.swarm-aggregation-completed.v1"  # onex-topic-allow: test fixture constant mirrors contract.yaml subscribe_topics

_HEALTH_EVENT: dict[str, Any] = {
    "run_id": _RUN_ID,
    "endpoint_health": {
        "ep-1": {"endpoint_status": "reachable", "latency_ms": 30},
        "ep-2": {"endpoint_status": "reachable", "latency_ms": 45},
    },
}

_DECOMPOSITION_EVENT: dict[str, Any] = {
    "run_id": _RUN_ID,
    "subtasks": [
        {
            "subtask_id": "st-A",
            "description": "Define data models",
            "model_affinity": "",
            "depends_on": [],
            "estimated_tokens": 120,
            "category": "code",
        },
        {
            "subtask_id": "st-B",
            "description": "Implement API routes",
            "model_affinity": "",
            "depends_on": ["st-A"],
            "estimated_tokens": 200,
            "category": "code",
        },
    ],
}

_ENDPOINTS_SELECTED_EVENT: dict[str, Any] = {
    "run_id": _RUN_ID,
    "assignments": {"st-A": "ep-1", "st-B": "ep-2"},
}

_FANOUT_COMPLETED_EVENT: dict[str, Any] = {
    "run_id": _RUN_ID,
    "dispatches": [
        {
            "subtask_id": "st-A",
            "endpoint_id": "ep-1",
            "status": "succeeded",
            "latency_ms": 800,
            "result_text": "models done",
            "failure_reason": "",
            "wave": 0,
            "model_id": "qwen3",
            "base_url": "https://endpoint-a.example.invalid",
        },
        {
            "subtask_id": "st-B",
            "endpoint_id": "ep-2",
            "status": "succeeded",
            "latency_ms": 1200,
            "result_text": "routes done",
            "failure_reason": "",
            "wave": 1,
            "model_id": "deepseek",
            "base_url": "https://endpoint-b.example.invalid",
        },
    ],
}

_AGGREGATION_COMPLETED_EVENT: dict[str, Any] = {
    "run_id": _RUN_ID,
    "aggregated_output": (
        "## Subtask: st-A\nmodels done\n\n## Subtask: st-B\nroutes done"
    ),
}


@pytest.fixture
def mock_bus() -> MagicMock:
    bus = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def state_store() -> _MemoryStateStore:
    return _MemoryStateStore()


@pytest.fixture
def handler(
    mock_bus: MagicMock, state_store: _MemoryStateStore
) -> HandlerSwarmDispatchOrchestrator:
    return HandlerSwarmDispatchOrchestrator(
        event_bus=mock_bus,
        state_store=state_store,
    )


@pytest.fixture
def dispatch_request() -> ModelSwarmDispatchRequest:
    return ModelSwarmDispatchRequest(
        task="Build a REST API with auth",
        endpoint_ids=("ep-1", "ep-2"),
        run_id=_RUN_ID,
        correlation_id=_CORR_ID,
    )


# ---------------------------------------------------------------------------
# Integration test: full FSM traversal via route_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFSMRoutingIntegration:
    async def test_full_fsm_traversal_received_to_completed(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        dispatch_request: ModelSwarmDispatchRequest,
        state_store: _MemoryStateStore,
    ) -> None:
        """Full integration: RECEIVED → HEALTH_CHECKED → DECOMPOSED →
        ENDPOINTS_SELECTED → DISPATCHING → AGGREGATING → COMPLETED."""

        # Step 1: handle_async — creates run state at RECEIVED, emits health-check cmd.
        # Returns None for non-terminal RECEIVED state (OMN-12151).
        result = await handler.handle_async(dispatch_request)
        assert result is None

        # State must be persisted after handle_async
        assert await state_store.exists(
            "node_swarm_dispatch_orchestrator", scope_id=_RUN_ID
        )

        # Step 2: health-completed event → HEALTH_CHECKED
        state = await handler.route_event(_HEALTH_COMPLETED_TOPIC, _HEALTH_EVENT)
        assert state.fsm_state == EnumSwarmOrchestratorState.HEALTH_CHECKED
        assert "ep-1" in state.endpoint_health

        # Step 3: decomposition-completed event → DECOMPOSED
        state = await handler.route_event(
            _DECOMPOSITION_COMPLETED_TOPIC, _DECOMPOSITION_EVENT
        )
        assert state.fsm_state == EnumSwarmOrchestratorState.DECOMPOSED
        assert len(state.subtasks) == 2

        # Step 4: endpoints-selected event → ENDPOINTS_SELECTED
        state = await handler.route_event(
            _ENDPOINTS_SELECTED_TOPIC, _ENDPOINTS_SELECTED_EVENT
        )
        assert state.fsm_state == EnumSwarmOrchestratorState.ENDPOINTS_SELECTED
        assert state.assignments == {"st-A": "ep-1", "st-B": "ep-2"}

        # Step 5: fanout-completed event → DISPATCHING
        state = await handler.route_event(
            _FANOUT_COMPLETED_TOPIC, _FANOUT_COMPLETED_EVENT
        )
        assert state.fsm_state == EnumSwarmOrchestratorState.DISPATCHING
        assert len(state.dispatches) == 2

        # Step 6: aggregation-completed event → AGGREGATING → COMPLETED
        # (route_event auto-advances through both transitions in one shot)
        state = await handler.route_event(
            _AGGREGATION_COMPLETED_TOPIC, _AGGREGATION_COMPLETED_EVENT
        )
        assert state.fsm_state == EnumSwarmOrchestratorState.COMPLETED
        assert "models done" in state.aggregated_output
        assert "routes done" in state.aggregated_output

    async def test_completed_state_persisted_after_each_step(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        dispatch_request: ModelSwarmDispatchRequest,
        state_store: _MemoryStateStore,
    ) -> None:
        """State store holds the latest FSM state after every route_event call."""
        await handler.handle_async(dispatch_request)

        await handler.route_event(_HEALTH_COMPLETED_TOPIC, _HEALTH_EVENT)
        envelope = await state_store.get(
            "node_swarm_dispatch_orchestrator", scope_id=_RUN_ID
        )
        assert envelope is not None
        assert envelope.data["fsm_state"] == "health_checked"

        await handler.route_event(_DECOMPOSITION_COMPLETED_TOPIC, _DECOMPOSITION_EVENT)
        envelope = await state_store.get(
            "node_swarm_dispatch_orchestrator", scope_id=_RUN_ID
        )
        assert envelope is not None
        assert envelope.data["fsm_state"] == "decomposed"

    async def test_missing_run_id_raises_missing_run_state_error(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
    ) -> None:
        """route_event raises MissingRunStateError when no state exists for run_id."""
        payload = {"run_id": "run-does-not-exist"}
        with pytest.raises(MissingRunStateError):
            await handler.route_event(_HEALTH_COMPLETED_TOPIC, payload)

    async def test_unmapped_topic_raises_value_error(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        dispatch_request: ModelSwarmDispatchRequest,
    ) -> None:
        """route_event raises ValueError for topics not in the routing map."""
        await handler.handle_async(dispatch_request)
        with pytest.raises(ValueError, match="No FSM routing for topic"):
            await handler.route_event(
                "onex.evt.omnimarket.unknown-topic.v1",  # onex-topic-allow: intentionally unknown topic for negative-path test
                {"run_id": _RUN_ID},
            )

    async def test_empty_run_id_raises_value_error(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
    ) -> None:
        """route_event raises ValueError when payload has no run_id."""
        with pytest.raises(ValueError, match="missing run_id"):
            await handler.route_event(_HEALTH_COMPLETED_TOPIC, {})

    async def test_topic_routing_map_covers_all_response_topics(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
    ) -> None:
        """All 5 response event topics must be registered in the routing map."""
        expected_fragments = [
            "swarm-endpoint-health-completed",
            "swarm-decomposition-completed",
            "swarm-endpoints-selected",
            "swarm-fanout-completed",
            "swarm-aggregation-completed",
        ]
        routing_topics = list(handler.subscribed_event_topics)
        for fragment in expected_fragments:
            assert any(fragment in t for t in routing_topics), (
                f"Missing routing entry for fragment {fragment!r}; "
                f"registered topics: {routing_topics}"
            )

    async def test_handle_async_persists_received_state(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        dispatch_request: ModelSwarmDispatchRequest,
        state_store: _MemoryStateStore,
    ) -> None:
        """handle_async must persist RECEIVED state before returning."""
        await handler.handle_async(dispatch_request)
        envelope = await state_store.get(
            "node_swarm_dispatch_orchestrator", scope_id=_RUN_ID
        )
        assert envelope is not None
        assert envelope.data["run_id"] == _RUN_ID
        assert envelope.data["fsm_state"] == "received"

    async def test_health_check_command_published_on_handle_async(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        dispatch_request: ModelSwarmDispatchRequest,
        mock_bus: MagicMock,
    ) -> None:
        """handle_async must publish the health-check command."""
        await handler.handle_async(dispatch_request)
        assert mock_bus.publish.called
        call_kwargs = mock_bus.publish.call_args_list[0][1]
        assert "swarm-check-endpoint-health" in call_kwargs["topic"]

    async def test_completed_event_published_after_aggregation(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        dispatch_request: ModelSwarmDispatchRequest,
        mock_bus: MagicMock,
    ) -> None:
        """After aggregation-completed, a swarm-dispatch-completed event is published."""
        await handler.handle_async(dispatch_request)
        await handler.route_event(_HEALTH_COMPLETED_TOPIC, _HEALTH_EVENT)
        await handler.route_event(_DECOMPOSITION_COMPLETED_TOPIC, _DECOMPOSITION_EVENT)
        await handler.route_event(_ENDPOINTS_SELECTED_TOPIC, _ENDPOINTS_SELECTED_EVENT)
        await handler.route_event(_FANOUT_COMPLETED_TOPIC, _FANOUT_COMPLETED_EVENT)
        await handler.route_event(
            _AGGREGATION_COMPLETED_TOPIC, _AGGREGATION_COMPLETED_EVENT
        )

        published_topics = [
            call[1]["topic"] for call in mock_bus.publish.call_args_list
        ]
        assert any("swarm-dispatch-completed" in t for t in published_topics), (
            f"Expected swarm-dispatch-completed in published topics: {published_topics}"
        )

    async def test_no_state_store_handle_async_still_works(
        self,
        mock_bus: MagicMock,
        dispatch_request: ModelSwarmDispatchRequest,
    ) -> None:
        """Without a state_store, handle_async runs without error and returns None (OMN-12151)."""
        handler_no_store = HandlerSwarmDispatchOrchestrator(event_bus=mock_bus)
        result = await handler_no_store.handle_async(dispatch_request)
        assert result is None
