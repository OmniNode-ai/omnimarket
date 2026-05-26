# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_subtask import (
    ModelSubtask,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_config import (
    ModelSwarmConfig,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_endpoint import (
    ModelSwarmEndpoint,
)


class ModelSwarmFanoutRequest(BaseModel):
    """Command accepted by the fanout orchestrator.

    The dispatch orchestrator sends ``endpoint_health`` (health-check results keyed
    by endpoint_id). Direct callers / tests may supply ``endpoints`` (full endpoint
    objects) instead. The handler resolves endpoints from the registry when only
    ``endpoint_health`` is provided.

    dispatch_mode and worker_assignments are intentionally absent — this node
    always publishes delegation-execute commands; it never does direct HTTP.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subtasks: tuple[ModelSubtask, ...]
    assignments: dict[str, str]  # subtask_id → endpoint_id
    # Full endpoint objects — populated by direct callers / tests.
    endpoints: tuple[ModelSwarmEndpoint, ...] = ()
    # Health dict forwarded by the dispatch orchestrator; handler resolves full
    # endpoint objects from the registry when ``endpoints`` is empty.
    endpoint_health: dict[str, Any] = {}
    config: ModelSwarmConfig
    correlation_id: str
    run_id: str
    endpoint_registry_hash: str = ""
    routing_policy_hash: str = ""
