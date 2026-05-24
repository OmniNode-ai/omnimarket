# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for swarm aggregator compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_aggregator_compute.models.model_decomposition import (
    ModelDecomposition,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_swarm_dispatch import (
    ModelSwarmDispatch,
)


class ModelSwarmAggregateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decomposition: ModelDecomposition
    dispatches: tuple[ModelSwarmDispatch, ...]
    mode: str = "concatenation"
    synthesis_output: str | None = None
    synthesis_model_id: str | None = None
    synthesis_input_hash: str | None = None
    correlation_id: str
