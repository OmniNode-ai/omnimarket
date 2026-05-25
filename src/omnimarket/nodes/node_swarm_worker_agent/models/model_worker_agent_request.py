# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.swarm.model_swarm_subtask_assignment import (
    ModelSwarmSubtaskAssignment,
)


class ModelWorkerAgentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    worker_id: str
    assignment: ModelSwarmSubtaskAssignment
