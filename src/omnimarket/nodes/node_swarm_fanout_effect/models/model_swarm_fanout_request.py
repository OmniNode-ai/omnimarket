# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_fanout_effect.models.enums import EnumDispatchMode
from omnimarket.nodes.node_swarm_fanout_effect.models.model_subtask import ModelSubtask
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_config import (
    ModelSwarmConfig,
)
from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_endpoint import (
    ModelSwarmEndpoint,
)


class ModelSwarmFanoutRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtasks: tuple[ModelSubtask, ...]
    assignments: dict[str, str]  # subtask_id → endpoint_id
    endpoints: tuple[ModelSwarmEndpoint, ...]
    config: ModelSwarmConfig
    correlation_id: str
    run_id: str
    dispatch_mode: EnumDispatchMode = EnumDispatchMode.DIRECT
    # maps subtask_id → worker_id for queue dispatch mode
    worker_assignments: dict[str, str] = {}
