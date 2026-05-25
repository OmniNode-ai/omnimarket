# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for swarm aggregator compute node.

Accepts two shapes:

1. **Direct / test callers** supply ``decomposition`` + ``dispatches`` (typed).
2. **Orchestrator** supplies ``subtasks`` + ``dispatches_json`` (serialised).

The handler checks which fields are populated and acts accordingly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_aggregator_compute.models.model_decomposition import (
    ModelDecomposition,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_subtask import (
    ModelSubtask,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_swarm_dispatch import (
    ModelSwarmDispatch,
)


class ModelSwarmAggregateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- Shape 1: typed objects (direct callers / tests) ---
    decomposition: ModelDecomposition | None = None
    dispatches: tuple[ModelSwarmDispatch, ...] = ()

    # --- Shape 2: orchestrator sends these ---
    subtasks: tuple[ModelSubtask, ...] = ()
    dispatches_json: str = ""

    mode: str = "concatenation"
    synthesis_output: str | None = None
    synthesis_model_id: str | None = None
    synthesis_input_hash: str | None = None
    correlation_id: str = ""
    run_id: str = ""
