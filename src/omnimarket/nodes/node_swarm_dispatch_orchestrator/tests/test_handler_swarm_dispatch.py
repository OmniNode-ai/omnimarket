# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerSwarmDispatchOrchestrator command payload building and terminal events."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_swarm_dispatch_orchestrator.handlers.handler_swarm_dispatch import (
    HandlerSwarmDispatchOrchestrator,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.enums import (
    EnumSwarmOrchestratorState,
    EnumSwarmRunStatus,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_orchestrator_state import (
    ModelEndpointHealth,
    ModelOrchestratorState,
    ModelSubtask,
    ModelSubtaskDispatch,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_swarm_dispatch_request import (
    ModelSwarmDispatchRequest,
)


@pytest.fixture
def mock_bus() -> MagicMock:
    bus = MagicMock()
    bus.publish = MagicMock()
    return bus


@pytest.fixture
def handler(mock_bus: MagicMock) -> HandlerSwarmDispatchOrchestrator:
    return HandlerSwarmDispatchOrchestrator(event_bus=mock_bus)


@pytest.fixture
def request_fixture() -> ModelSwarmDispatchRequest:
    return ModelSwarmDispatchRequest(
        task="Implement OAuth2 login flow",
        endpoint_ids=("ep-1", "ep-2"),
        run_id="run-999",
        correlation_id="corr-999",
    )


_HEALTH_EVENT = {
    "endpoint_health": {
        "ep-1": {"endpoint_status": "reachable", "latency_ms": 30},
        "ep-2": {"endpoint_status": "reachable", "latency_ms": 40},
    }
}

_DECOMPOSE_EVENT = {
    "subtasks": [
        {
            "subtask_id": "st-A",
            "description": "Auth routes",
            "model_affinity": "",
            "depends_on": [],
            "estimated_tokens": 150,
            "category": "code",
        },
    ]
}

_SELECT_EVENT = {"assignments": {"st-A": "ep-1"}}

_FANOUT_SUCCEEDED = {
    "dispatches": [
        {
            "subtask_id": "st-A",
            "endpoint_id": "ep-1",
            "status": "succeeded",
            "latency_ms": 900,
            "result_text": "auth done",
            "failure_reason": "",
            "wave": 0,
            "model_id": "qwen3",
            "base_url": "https://endpoint-a.example.invalid",
        },
    ]
}

_FANOUT_ALL_FAILED = {
    "dispatches": [
        {
            "subtask_id": "st-A",
            "endpoint_id": "ep-1",
            "status": "failed",
            "latency_ms": 100,
            "result_text": "",
            "failure_reason": "connection refused",
            "wave": 0,
            "model_id": "qwen3",
            "base_url": "https://endpoint-a.example.invalid",
        },
    ]
}

_AGGREGATE_EVENT = {"aggregated_output": "## Subtask: st-A\nauth done"}


def _received_state(
    run_id: str = "run-999", correlation_id: str = "corr-999", task: str = "test task"
) -> ModelOrchestratorState:
    return ModelOrchestratorState(
        fsm_state=EnumSwarmOrchestratorState.RECEIVED,
        run_id=run_id,
        correlation_id=correlation_id,
        original_task=task,
    )


class TestCommandPayloads:
    def test_health_command_published_with_endpoint_ids(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        request_fixture: ModelSwarmDispatchRequest,
    ) -> None:
        state = _received_state(
            run_id=request_fixture.run_id, task=request_fixture.task
        )
        _, publishes = handler.transition_received(state, request_fixture)
        assert len(publishes) == 1
        topic, payload = publishes[0]
        assert "swarm-check-endpoint-health" in topic
        assert "ep-1" in payload["endpoint_ids"]
        assert payload["run_id"] == "run-999"

    def test_decompose_command_includes_planner_output(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
    ) -> None:
        state = _received_state()
        _, publishes = handler.transition_health_checked(
            state, _HEALTH_EVENT, planner_output="plan text", planner_model_id="qwen3"
        )
        assert len(publishes) == 1
        topic, payload = publishes[0]
        assert "swarm-decompose" in topic
        assert payload["planner_output"] == "plan text"
        assert payload["planner_model_id"] == "qwen3"
        assert payload["original_task"] == "test task"

    def test_select_endpoints_command_includes_subtasks_and_health(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
    ) -> None:
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.HEALTH_CHECKED,
            run_id="run-999",
            correlation_id="corr-999",
            original_task="test task",
            endpoint_health={
                "ep-1": ModelEndpointHealth(
                    endpoint_id="ep-1", status="reachable", latency_ms=30
                )
            },
        )
        _, publishes = handler.transition_decomposed(state, _DECOMPOSE_EVENT)
        assert len(publishes) == 1
        topic, payload = publishes[0]
        assert "swarm-select-endpoints" in topic
        assert len(payload["subtasks"]) == 1
        assert "endpoint_health" in payload

    def test_fanout_command_includes_assignments(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
    ) -> None:
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.DECOMPOSED,
            run_id="run-999",
            correlation_id="corr-999",
            original_task="test task",
            subtasks=(ModelSubtask(subtask_id="st-A", description="Auth routes"),),
            endpoint_health={
                "ep-1": ModelEndpointHealth(
                    endpoint_id="ep-1", status="reachable", latency_ms=30
                )
            },
        )
        _, publishes = handler.transition_endpoints_selected(state, _SELECT_EVENT)
        assert len(publishes) == 1
        topic, payload = publishes[0]
        assert "swarm-fanout" in topic
        assert payload["assignments"] == {"st-A": "ep-1"}

    def test_aggregate_command_includes_dispatch_mode(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
    ) -> None:
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.ENDPOINTS_SELECTED,
            run_id="run-999",
            correlation_id="corr-999",
            original_task="test task",
            subtasks=(ModelSubtask(subtask_id="st-A", description="Auth routes"),),
            assignments={"st-A": "ep-1"},
            endpoint_health={
                "ep-1": ModelEndpointHealth(
                    endpoint_id="ep-1", status="reachable", latency_ms=30
                )
            },
        )
        _, publishes = handler.transition_dispatching(state, _FANOUT_SUCCEEDED)
        assert len(publishes) == 1
        topic, payload = publishes[0]
        assert "swarm-aggregate" in topic
        assert payload["mode"] == "concatenation"


class TestTerminalEventEmission:
    def _build_aggregating_state(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        fanout_event: dict[str, list[dict[str, Any]]],
        run_id: str = "run-T",
    ) -> ModelOrchestratorState:
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.DISPATCHING,
            run_id=run_id,
            correlation_id="corr-T",
            original_task="task",
            subtasks=(ModelSubtask(subtask_id="st-A", description="step"),),
            assignments={"st-A": "ep-1"},
            endpoint_health={
                "ep-1": ModelEndpointHealth(endpoint_id="ep-1", status="reachable")
            },
            dispatches=tuple(
                ModelSubtaskDispatch(
                    subtask_id=str(d["subtask_id"]),
                    endpoint_id=str(d["endpoint_id"]),
                    status=str(d["status"]),
                    latency_ms=int(d["latency_ms"]),
                    result_text=str(d["result_text"]),
                    failure_reason=str(d["failure_reason"]),
                    wave=int(d["wave"]),
                    model_id=str(d["model_id"]),
                    base_url=str(d["base_url"]),
                )
                for d in fanout_event["dispatches"]
            ),
        )
        aggregating_state, _ = handler.transition_aggregating(state, _AGGREGATE_EVENT)
        return aggregating_state

    def test_completed_event_published_on_success(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
    ) -> None:
        aggregating_state = self._build_aggregating_state(handler, _FANOUT_SUCCEEDED)
        _, publishes = handler.transition_completed(aggregating_state)
        assert len(publishes) == 1
        topic, payload = publishes[0]
        assert "swarm-dispatch-completed" in topic
        assert payload["status"] == EnumSwarmRunStatus.SUCCEEDED.value
        assert payload["succeeded_count"] == 1
        assert payload["failed_count"] == 0

    def test_failed_event_published_on_error(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
    ) -> None:
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.RECEIVED,
            run_id="run-F",
            correlation_id="corr-F",
            original_task="task",
        )
        _failed_state, publishes = handler.transition_failed(
            state, "something exploded"
        )
        assert len(publishes) == 1
        topic, payload = publishes[0]
        assert "swarm-dispatch-failed" in topic
        assert payload["error"] == "something exploded"

    def test_degraded_status_when_some_subtasks_fail(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
    ) -> None:
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.DISPATCHING,
            run_id="run-D",
            correlation_id="corr-D",
            original_task="task",
            subtasks=(
                ModelSubtask(subtask_id="st-1", description="a"),
                ModelSubtask(subtask_id="st-2", description="b"),
            ),
            assignments={"st-1": "ep-1", "st-2": "ep-2"},
            endpoint_health={
                "ep-1": ModelEndpointHealth(endpoint_id="ep-1", status="reachable"),
                "ep-2": ModelEndpointHealth(endpoint_id="ep-2", status="reachable"),
            },
            dispatches=(
                ModelSubtaskDispatch(
                    subtask_id="st-1",
                    endpoint_id="ep-1",
                    status="succeeded",
                    model_id="m1",
                    base_url="http://x",  # local-path-ok
                ),
                ModelSubtaskDispatch(
                    subtask_id="st-2",
                    endpoint_id="ep-2",
                    status="failed",
                    failure_reason="timeout",
                    model_id="m2",
                    base_url="http://y",  # local-path-ok
                ),
            ),
        )
        agg_state, _ = handler.transition_aggregating(state, _AGGREGATE_EVENT)
        _, publishes = handler.transition_completed(agg_state)
        assert len(publishes) == 1
        _topic, payload = publishes[0]
        assert payload["status"] == EnumSwarmRunStatus.DEGRADED.value
        assert payload["succeeded_count"] == 1
        assert payload["failed_count"] == 1

    def test_failed_status_when_all_subtasks_fail(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
    ) -> None:
        aggregating_state = self._build_aggregating_state(handler, _FANOUT_ALL_FAILED)
        _, publishes = handler.transition_completed(aggregating_state)
        assert len(publishes) == 1
        _, payload = publishes[0]
        assert payload["status"] == EnumSwarmRunStatus.FAILED.value
