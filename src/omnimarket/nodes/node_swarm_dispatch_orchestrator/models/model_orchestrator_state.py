# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Mutable orchestrator session state threaded through FSM transitions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.enums import (
    EnumSwarmOrchestratorState,
)


class ModelEndpointHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_id: str
    status: str
    latency_ms: int | None = None
    error: str | None = None


class ModelSubtask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtask_id: str
    description: str
    model_affinity: str = ""
    depends_on: tuple[str, ...] = ()
    estimated_tokens: int = 0
    category: str = "general"


class ModelSubtaskDispatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtask_id: str
    endpoint_id: str
    status: str
    latency_ms: int = 0
    result_text: str = ""
    failure_reason: str = ""
    wave: int = 0
    model_id: str = ""
    base_url: str = ""


class ModelOrchestratorState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fsm_state: EnumSwarmOrchestratorState = EnumSwarmOrchestratorState.RECEIVED
    run_id: str = ""
    correlation_id: str = ""
    original_task: str = ""
    endpoint_health: dict[str, ModelEndpointHealth] = {}
    subtasks: tuple[ModelSubtask, ...] = ()
    assignments: dict[str, str] = {}
    dispatches: tuple[ModelSubtaskDispatch, ...] = ()
    aggregated_output: str = ""
    error: str = ""
    total_latency_ms: int = 0
    dispatch_wall_latency_ms: int = 0
    max_subtasks: int = 6

    def with_state(self, state: EnumSwarmOrchestratorState) -> ModelOrchestratorState:
        return self.model_copy(update={"fsm_state": state})

    def with_max_subtasks(self, max_subtasks: int) -> ModelOrchestratorState:
        return self.model_copy(update={"max_subtasks": max_subtasks})

    def with_health(
        self, health: dict[str, ModelEndpointHealth]
    ) -> ModelOrchestratorState:
        return self.model_copy(
            update={
                "fsm_state": EnumSwarmOrchestratorState.HEALTH_CHECKED,
                "endpoint_health": health,
            }
        )

    def with_subtasks(
        self, subtasks: tuple[ModelSubtask, ...]
    ) -> ModelOrchestratorState:
        return self.model_copy(
            update={
                "fsm_state": EnumSwarmOrchestratorState.DECOMPOSED,
                "subtasks": subtasks,
            }
        )

    def with_assignments(self, assignments: dict[str, str]) -> ModelOrchestratorState:
        return self.model_copy(
            update={
                "fsm_state": EnumSwarmOrchestratorState.ENDPOINTS_SELECTED,
                "assignments": assignments,
            }
        )

    def with_dispatches(
        self,
        dispatches: tuple[ModelSubtaskDispatch, ...],
        dispatch_wall_latency_ms: int = 0,
    ) -> ModelOrchestratorState:
        return self.model_copy(
            update={
                "fsm_state": EnumSwarmOrchestratorState.DISPATCHING,
                "dispatches": dispatches,
                "dispatch_wall_latency_ms": dispatch_wall_latency_ms,
            }
        )

    def with_aggregated(
        self, aggregated_output: str, total_latency_ms: int
    ) -> ModelOrchestratorState:
        return self.model_copy(
            update={
                "fsm_state": EnumSwarmOrchestratorState.AGGREGATING,
                "aggregated_output": aggregated_output,
                "total_latency_ms": total_latency_ms,
            }
        )

    def with_error(self, error: str) -> ModelOrchestratorState:
        return self.model_copy(
            update={"fsm_state": EnumSwarmOrchestratorState.FAILED, "error": error}
        )
