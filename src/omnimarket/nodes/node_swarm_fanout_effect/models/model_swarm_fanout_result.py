# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_fanout_effect.models.model_swarm_dispatch import (
    ModelSwarmDispatch,
)


class ModelSwarmFanoutResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatches: tuple[ModelSwarmDispatch, ...]
    wall_latency_ms: int
    sum_subtask_latency_ms: int
    run_id: str = ""
