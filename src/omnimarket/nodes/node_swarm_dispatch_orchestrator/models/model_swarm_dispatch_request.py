# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for swarm dispatch orchestrator."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.model_orchestrator_state import (
    ModelEndpointHealth,
    ModelSubtask,
)


class ModelSwarmConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_parallel_subtasks: int = 4
    max_subtasks_per_endpoint: int = 2
    per_endpoint_timeout_seconds: int = 120
    total_run_timeout_seconds: int = 600
    retry_policy_max_retries: int = 1
    fallback_policy_enabled: bool = True


class ModelSwarmDispatchRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str = Field(..., min_length=1)
    endpoint_ids: tuple[str, ...] = ()
    max_subtasks: int = 6
    decompose: bool = True
    correlation_id: str
    run_id: str
    config: ModelSwarmConfig | None = None
    # Caller hint for model-tier routing; not consumed by the FSM itself.
    model_tier: str | None = None


class ModelSwarmHealthCheckCommand(BaseModel):
    """Command payload for swarm-check-endpoint-health."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_ids: tuple[str, ...]
    correlation_id: str
    run_id: str


class ModelSwarmDecomposeCommand(BaseModel):
    """Command payload for swarm-decompose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    planner_output: str
    planner_model_id: str
    planner_output_hash: str
    endpoint_ids: tuple[str, ...]
    original_task: str
    correlation_id: str
    run_id: str
    decompose: bool = True
    max_subtasks: int = 6


class ModelSwarmSelectEndpointsCommand(BaseModel):
    """Command payload for swarm-select-endpoints."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subtasks: tuple[ModelSubtask, ...]
    endpoint_health: dict[str, ModelEndpointHealth]
    correlation_id: str
    run_id: str


class ModelSwarmFanoutCommand(BaseModel):
    """Command payload for swarm-fanout."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subtasks: tuple[ModelSubtask, ...]
    assignments: dict[str, str]
    endpoint_health: dict[str, ModelEndpointHealth]
    config: ModelSwarmConfig
    correlation_id: str
    run_id: str


class ModelSwarmAggregateCommand(BaseModel):
    """Command payload for swarm-aggregate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subtasks: tuple[ModelSubtask, ...]
    dispatches_json: str
    mode: str = "concatenation"
    synthesis_output: str | None = None
    correlation_id: str
    run_id: str
