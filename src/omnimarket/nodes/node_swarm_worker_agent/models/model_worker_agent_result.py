# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.swarm.model_swarm_subtask_result import ModelSwarmSubtaskResult


class ModelWorkerAgentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtask_result: ModelSwarmSubtaskResult
    published_topic: str
