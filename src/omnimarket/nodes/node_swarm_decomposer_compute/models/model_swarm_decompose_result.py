# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_swarm_decomposer_compute.models.enums import (
    EnumDecompositionStatus,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_decomposition import (
    ModelDecomposition,
)


class ModelSwarmDecomposeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decomposition: ModelDecomposition
    status: EnumDecompositionStatus
    warnings: tuple[str, ...] = Field(default=())
    run_id: str = Field(default="")
