# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for swarm dispatch orchestrator models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.enums import (
    EnumAggregationMode,
    EnumSwarmOrchestratorState,
    EnumSwarmRunStatus,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_orchestrator_state import (
    ModelEndpointHealth,
    ModelOrchestratorState,
    ModelSubtask,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_swarm_dispatch_request import (
    ModelSwarmAggregateCommand,
    ModelSwarmConfig,
    ModelSwarmDecomposeCommand,
    ModelSwarmDispatchRequest,
    ModelSwarmFanoutCommand,
    ModelSwarmHealthCheckCommand,
    ModelSwarmSelectEndpointsCommand,
)
from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_swarm_dispatch_result import (
    ModelSwarmDispatchResult,
)


class TestEnums:
    def test_orchestrator_states_all_present(self) -> None:
        states = {s.value for s in EnumSwarmOrchestratorState}
        assert "received" in states
        assert "completed" in states
        assert "failed" in states
        assert len(states) == 8

    def test_run_status_values(self) -> None:
        assert EnumSwarmRunStatus.SUCCEEDED.value == "succeeded"
        assert EnumSwarmRunStatus.DEGRADED.value == "degraded"
        assert EnumSwarmRunStatus.FAILED.value == "failed"

    def test_aggregation_mode_values(self) -> None:
        assert EnumAggregationMode.CONCATENATION.value == "concatenation"
        assert EnumAggregationMode.SYNTHESIS.value == "synthesis"


class TestModelSwarmDispatchRequest:
    def test_valid_minimal(self) -> None:
        req = ModelSwarmDispatchRequest(
            task="do something", run_id="r1", correlation_id="c1"
        )
        assert req.task == "do something"
        assert req.endpoint_ids == ()
        assert req.decompose is True
        assert req.config is None

    def test_valid_with_config(self) -> None:
        config = ModelSwarmConfig(max_parallel_subtasks=8)
        req = ModelSwarmDispatchRequest(
            task="complex task",
            endpoint_ids=("ep-1",),
            run_id="r2",
            correlation_id="c2",
            config=config,
        )
        assert req.config is not None
        assert req.config.max_parallel_subtasks == 8

    def test_empty_task_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelSwarmDispatchRequest(task="", run_id="r1", correlation_id="c1")

    def test_frozen(self) -> None:
        req = ModelSwarmDispatchRequest(task="task", run_id="r1", correlation_id="c1")
        with pytest.raises(ValidationError):
            req.task = "mutated"  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelSwarmDispatchRequest(
                task="t", run_id="r", correlation_id="c", extra_field="x"
            )  # type: ignore[call-arg]


class TestModelSwarmDispatchResult:
    def test_valid_result(self) -> None:
        result = ModelSwarmDispatchResult(
            run_id="r1",
            correlation_id="c1",
            status=EnumSwarmRunStatus.SUCCEEDED,
            aggregated_output="output text",
            subtask_count=3,
            succeeded_count=3,
            failed_count=0,
            skipped_count=0,
            total_latency_ms=2500,
        )
        assert result.models_used == ()
        assert result.error == ""

    def test_frozen(self) -> None:
        result = ModelSwarmDispatchResult(
            run_id="r1",
            correlation_id="c1",
            status=EnumSwarmRunStatus.FAILED,
            aggregated_output="",
            subtask_count=0,
            succeeded_count=0,
            failed_count=1,
            skipped_count=0,
            total_latency_ms=0,
        )
        with pytest.raises(ValidationError):
            result.error = "new error"  # type: ignore[misc]


class TestModelOrchestratorState:
    def test_default_state_is_received(self) -> None:
        state = ModelOrchestratorState()
        assert state.fsm_state == EnumSwarmOrchestratorState.RECEIVED

    def test_with_state_returns_new_instance(self) -> None:
        state = ModelOrchestratorState()
        new_state = state.with_state(EnumSwarmOrchestratorState.COMPLETED)
        assert new_state.fsm_state == EnumSwarmOrchestratorState.COMPLETED
        assert state.fsm_state == EnumSwarmOrchestratorState.RECEIVED

    def test_with_health(self) -> None:
        state = ModelOrchestratorState(run_id="r1", correlation_id="c1")
        health = {"ep-1": ModelEndpointHealth(endpoint_id="ep-1", status="reachable")}
        new_state = state.with_health(health)
        assert new_state.fsm_state == EnumSwarmOrchestratorState.HEALTH_CHECKED
        assert "ep-1" in new_state.endpoint_health

    def test_with_subtasks(self) -> None:
        state = ModelOrchestratorState(run_id="r1", correlation_id="c1")
        subtasks = (ModelSubtask(subtask_id="st-1", description="desc"),)
        new_state = state.with_subtasks(subtasks)
        assert new_state.fsm_state == EnumSwarmOrchestratorState.DECOMPOSED
        assert len(new_state.subtasks) == 1

    def test_with_assignments(self) -> None:
        state = ModelOrchestratorState(run_id="r1", correlation_id="c1")
        new_state = state.with_assignments({"st-1": "ep-1"})
        assert new_state.fsm_state == EnumSwarmOrchestratorState.ENDPOINTS_SELECTED
        assert new_state.assignments["st-1"] == "ep-1"

    def test_with_error(self) -> None:
        state = ModelOrchestratorState(run_id="r1", correlation_id="c1")
        new_state = state.with_error("connection timeout")
        assert new_state.fsm_state == EnumSwarmOrchestratorState.FAILED
        assert new_state.error == "connection timeout"

    def test_with_aggregated(self) -> None:
        state = ModelOrchestratorState(run_id="r1", correlation_id="c1")
        new_state = state.with_aggregated("output text", 3000)
        assert new_state.fsm_state == EnumSwarmOrchestratorState.AGGREGATING
        assert new_state.aggregated_output == "output text"
        assert new_state.total_latency_ms == 3000

    def test_immutability(self) -> None:
        state = ModelOrchestratorState()
        with pytest.raises(ValidationError):
            state.run_id = "mutated"  # type: ignore[misc]


class TestCommandModels:
    def test_health_check_command(self) -> None:
        cmd = ModelSwarmHealthCheckCommand(
            endpoint_ids=("ep-1", "ep-2"),
            correlation_id="c1",
            run_id="r1",
        )
        assert len(cmd.endpoint_ids) == 2
        assert cmd.correlation_id == "c1"

    def test_decompose_command(self) -> None:
        cmd = ModelSwarmDecomposeCommand(
            planner_output="plan",
            planner_model_id="qwen3",
            planner_output_hash="abc123",
            endpoint_ids=("ep-1",),
            original_task="build api",
            correlation_id="c1",
            run_id="r1",
        )
        assert cmd.decompose is True
        assert cmd.max_subtasks == 6

    def test_select_endpoints_command(self) -> None:
        subtask = ModelSubtask(subtask_id="st-1", description="desc")
        health = {"ep-1": ModelEndpointHealth(endpoint_id="ep-1", status="reachable")}
        cmd = ModelSwarmSelectEndpointsCommand(
            subtasks=(subtask,),
            endpoint_health=health,
            correlation_id="c1",
            run_id="r1",
        )
        assert len(cmd.subtasks) == 1

    def test_fanout_command(self) -> None:
        subtask = ModelSubtask(subtask_id="st-1", description="desc")
        health = {"ep-1": ModelEndpointHealth(endpoint_id="ep-1", status="reachable")}
        config = ModelSwarmConfig()
        cmd = ModelSwarmFanoutCommand(
            subtasks=(subtask,),
            assignments={"st-1": "ep-1"},
            endpoint_health=health,
            config=config,
            correlation_id="c1",
            run_id="r1",
        )
        assert cmd.assignments["st-1"] == "ep-1"

    def test_aggregate_command_defaults(self) -> None:
        subtask = ModelSubtask(subtask_id="st-1", description="desc")
        cmd = ModelSwarmAggregateCommand(
            subtasks=(subtask,),
            dispatches_json="[]",
            correlation_id="c1",
            run_id="r1",
        )
        assert cmd.mode == "concatenation"
        assert cmd.synthesis_output is None
