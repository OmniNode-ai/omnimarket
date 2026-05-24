# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelSubtask — local copy for aggregator node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_aggregator_compute.models.enums import (
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
