# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_swarm_decomposer_compute.models.enums import (
    EnumDecompositionStatus,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_subtask import (
    ModelSubtask,
)


class ModelDecomposition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    original_task: str
    original_task_hash: str
    subtasks: tuple[ModelSubtask, ...]
    decomposition_model: str
    decomposition_endpoint_id: str
    decomposition_latency_ms: int
    decomposition_status: EnumDecompositionStatus
    decomposition_run_id: str
    correlation_id: str = Field(default="")
    warnings: tuple[str, ...] = Field(default=())
