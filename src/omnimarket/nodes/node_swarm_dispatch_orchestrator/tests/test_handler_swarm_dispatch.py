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


class TestHandleAsyncReturnsNone:
    """Verify handle_async returns None for non-terminal FSM states (OMN-12151).

    The swarm dispatch orchestrator is a multi-step FSM.  After the initial
    swarm-dispatch command arrives, the orchestrator is only in the RECEIVED
    state — it has emitted one sub-command (swarm-check-endpoint-health) and is
    waiting for response events on other topics.  Returning a non-None result
    at this point would cause DispatchResultApplier to publish a premature
    terminal event, short-circuiting the FSM before any real work is done.
    """

    @pytest.mark.asyncio
    async def test_handle_async_returns_none_for_received_state(
        self,
        mock_bus: MagicMock,
        request_fixture: ModelSwarmDispatchRequest,
    ) -> None:
        """handle_async must return None so the result applier skips terminal publish."""
        from unittest.mock import AsyncMock

        mock_bus.publish = AsyncMock()
        handler = HandlerSwarmDispatchOrchestrator(event_bus=mock_bus)

        await handler.handle_async(request_fixture)

    @pytest.mark.asyncio
    async def test_handle_async_still_flushes_health_check_command(
        self,
        mock_bus: MagicMock,
        request_fixture: ModelSwarmDispatchRequest,
    ) -> None:
        """handle_async must still publish the health-check sub-command even though it returns None."""
        from unittest.mock import AsyncMock

        publish_mock = AsyncMock()
        mock_bus.publish = publish_mock
        handler = HandlerSwarmDispatchOrchestrator(event_bus=mock_bus)

        await handler.handle_async(request_fixture)

        assert publish_mock.call_count == 1, (
            "Expected exactly one publish call for the health-check sub-command"
        )
        call_kwargs = publish_mock.call_args
        topic = call_kwargs.kwargs.get(
            "topic", call_kwargs.args[0] if call_kwargs.args else ""
        )
        assert "swarm-check-endpoint-health" in topic, (
            f"Expected health-check topic, got: {topic!r}"
        )

    @pytest.mark.asyncio
    async def test_handle_async_returns_none_on_exception(
        self,
        mock_bus: MagicMock,
        request_fixture: ModelSwarmDispatchRequest,
    ) -> None:
        """handle_async must return None even when an exception occurs during RECEIVED.

        The transition_failed method already published a swarm-dispatch-failed
        terminal event via _flush; returning None here prevents the result applier
        from also firing a terminal event.
        """
        from unittest.mock import AsyncMock, patch

        mock_bus.publish = AsyncMock()
        handler = HandlerSwarmDispatchOrchestrator(event_bus=mock_bus)

        with patch.object(
            handler,
            "transition_received",
            side_effect=RuntimeError("injected failure"),
        ):
            await handler.handle_async(request_fixture)


class TestMaxSubtasksThreading:
    """max_subtasks from request flows through to the decompose command."""

    def test_max_subtasks_stored_in_state(
        self, handler: HandlerSwarmDispatchOrchestrator
    ) -> None:
        req = ModelSwarmDispatchRequest(
            task="demo task",
            endpoint_ids=("ep-1", "ep-2", "ep-3", "ep-4", "ep-5", "ep-6"),
            max_subtasks=6,
            run_id="run-ms",
            correlation_id="corr-ms",
        )
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.RECEIVED,
            run_id=req.run_id,
            correlation_id=req.correlation_id,
            original_task=req.task,
        )
        new_state, _ = handler.transition_received(state, req)
        assert new_state.max_subtasks == 6

    def test_max_subtasks_propagates_to_decompose_command(
        self, handler: HandlerSwarmDispatchOrchestrator
    ) -> None:
        req = ModelSwarmDispatchRequest(
            task="demo task",
            endpoint_ids=("ep-1", "ep-2"),
            max_subtasks=6,
            run_id="run-ms2",
            correlation_id="corr-ms2",
        )
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.RECEIVED,
            run_id=req.run_id,
            correlation_id=req.correlation_id,
            original_task=req.task,
        )
        state_after_received, _ = handler.transition_received(state, req)
        _state, publishes = handler.transition_health_checked(
            state_after_received, _HEALTH_EVENT
        )
        assert len(publishes) == 1
        _, decompose_payload = publishes[0]
        assert decompose_payload["max_subtasks"] == 6

    def test_default_max_subtasks_is_six(
        self, handler: HandlerSwarmDispatchOrchestrator
    ) -> None:
        req = ModelSwarmDispatchRequest(
            task="demo task",
            endpoint_ids=("ep-1",),
            run_id="run-ms3",
            correlation_id="corr-ms3",
        )
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.RECEIVED,
            run_id=req.run_id,
            correlation_id=req.correlation_id,
            original_task=req.task,
        )
        new_state, _ = handler.transition_received(state, req)
        assert new_state.max_subtasks == 6


class TestCostFieldsInCompletedPayload:
    """Completed payload includes T4 cost/savings/speedup fields."""

    def _build_aggregating_state_with_latency(
        self,
        handler: HandlerSwarmDispatchOrchestrator,
        wall_latency_ms: int,
        subtask_latency_ms: int,
    ) -> ModelOrchestratorState:
        fanout_event: dict[str, Any] = {
            "dispatches": [
                {
                    "subtask_id": f"st-{i}",
                    "endpoint_id": f"ep-{i}",
                    "status": "succeeded",
                    "latency_ms": subtask_latency_ms,
                    "result_text": "done",
                    "failure_reason": "",
                    "wave": 0,
                    "model_id": f"model-{i}",
                    "base_url": f"https://ep-{i}.example.invalid",
                }
                for i in range(6)
            ],
            "wall_latency_ms": wall_latency_ms,
        }
        state = ModelOrchestratorState(
            fsm_state=EnumSwarmOrchestratorState.ENDPOINTS_SELECTED,
            run_id="run-cost",
            correlation_id="corr-cost",
            original_task="task",
            subtasks=tuple(
                ModelSubtask(subtask_id=f"st-{i}", description=f"sub {i}")
                for i in range(6)
            ),
            assignments={f"st-{i}": f"ep-{i}" for i in range(6)},
            endpoint_health={},
        )
        dispatching_state, _ = handler.transition_dispatching(state, fanout_event)
        agg_state, _ = handler.transition_aggregating(
            dispatching_state, {"aggregated_output": "merged"}
        )
        return agg_state

    def test_total_cost_usd_is_zero(
        self, handler: HandlerSwarmDispatchOrchestrator
    ) -> None:
        agg_state = self._build_aggregating_state_with_latency(handler, 8000, 5000)
        _, publishes = handler.transition_completed(agg_state)
        _, payload = publishes[0]
        assert payload["total_cost_usd"] == 0.0

    def test_cloud_equivalent_cost_positive(
        self, handler: HandlerSwarmDispatchOrchestrator
    ) -> None:
        agg_state = self._build_aggregating_state_with_latency(handler, 8000, 5000)
        _, publishes = handler.transition_completed(agg_state)
        _, payload = publishes[0]
        assert payload["cloud_equivalent_cost_usd"] > 0.0
        assert payload["savings_usd"] == payload["cloud_equivalent_cost_usd"]

    def test_parallelism_speedup_ratio_gt_one_when_parallel(
        self, handler: HandlerSwarmDispatchOrchestrator
    ) -> None:
        # 6 subtasks x 5s each = 30s serial; wall = 8s parallel -> speedup ~3.75
        agg_state = self._build_aggregating_state_with_latency(
            handler, wall_latency_ms=8000, subtask_latency_ms=5000
        )
        _, publishes = handler.transition_completed(agg_state)
        _, payload = publishes[0]
        assert payload["parallelism_speedup_ratio"] > 1.0

    def test_dispatch_wall_latency_in_payload(
        self, handler: HandlerSwarmDispatchOrchestrator
    ) -> None:
        agg_state = self._build_aggregating_state_with_latency(
            handler, wall_latency_ms=8000, subtask_latency_ms=5000
        )
        _, publishes = handler.transition_completed(agg_state)
        _, payload = publishes[0]
        assert payload["dispatch_wall_latency_ms"] == 8000
