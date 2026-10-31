# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Response model for the build loop state reducer handler."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_intent import (
    ModelBuildLoopIntent,
)
from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_state import (
    ModelBuildLoopState,
)


class ModelLoopStateReduceResult(BaseModel):
    """The new FSM state and the intents to emit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelBuildLoopState = Field(..., description="New FSM state.")
    intents: list[ModelBuildLoopIntent] = Field(
        default_factory=list, description="Intents for the orchestrator to route."
    )


__all__: list[str] = ["ModelLoopStateReduceResult"]
