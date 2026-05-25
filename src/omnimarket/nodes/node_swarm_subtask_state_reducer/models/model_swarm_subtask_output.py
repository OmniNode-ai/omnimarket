# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for the swarm subtask state reducer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_projection_freshness import (
    ModelProjectionFreshness,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_subtask_state import (
    ModelSubtaskState,
    ModelSwarmRunState,
)


class ModelSwarmSubtaskReducerOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    new_state: ModelSwarmRunState
    changed_subtask: ModelSubtaskState | None = None
    state_changed: bool = False
    projection_freshness: ModelProjectionFreshness | None = None


__all__: list[str] = ["ModelSwarmSubtaskReducerOutput"]
