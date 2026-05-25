# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_dispatch import (
    ModelSwarmDispatch,
)


class ModelSwarmFanoutResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatches: tuple[ModelSwarmDispatch, ...]
    wall_latency_ms: int
    sum_subtask_latency_ms: int
    run_id: str = ""

    # Terminal event payload fields (swarm-fanout-completed.v1)
    completed_count: int = 0
    failed_count: int = 0
    degraded: bool = False
    aggregation_mode: str = "collect_all"
    endpoint_registry_hash: str = ""
    routing_policy_hash: str = ""
    projection_ref: str = ""
