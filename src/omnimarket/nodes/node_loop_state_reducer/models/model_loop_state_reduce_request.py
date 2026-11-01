# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for the build loop state reducer handler."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_event import (
    ModelBuildLoopEvent,
)
from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_state import (
    ModelBuildLoopState,
)


class ModelLoopStateReduceRequest(BaseModel):
    """The current FSM state and the event to apply to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelBuildLoopState = Field(..., description="Current FSM state.")
    event: ModelBuildLoopEvent = Field(
        ..., description="Event from a completed build loop phase."
    )


__all__: list[str] = ["ModelLoopStateReduceRequest"]
