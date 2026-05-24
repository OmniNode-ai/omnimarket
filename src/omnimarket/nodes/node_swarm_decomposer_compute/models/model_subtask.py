# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_decomposer_compute.models.enums import (
    EnumSubtaskCategory,
)


class ModelSubtask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtask_id: str
    description: str
    model_affinity: str
    depends_on: tuple[str, ...] = ()
    estimated_tokens: int = 0
    token_estimation_method: str = "char_ratio"
    category: EnumSubtaskCategory = EnumSubtaskCategory.GENERAL

    @staticmethod
    def make_subtask_id(
        original_task_hash: str, subtask_index: int, description: str
    ) -> str:
        description_hash = hashlib.sha256(description.encode()).hexdigest()
        combined = original_task_hash + str(subtask_index) + description_hash
        return hashlib.sha256(combined.encode()).hexdigest()[:16]
