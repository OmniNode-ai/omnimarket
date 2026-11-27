# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The result of one fold (OMN-26706)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_intent import (
    ModelLaneWorkIntent,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_state import (
    ModelLaneWorkReducerState,
)


class ModelLaneWorkReduceResult(BaseModel):
    """New state and intents. A refused input is an outcome, not a state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["transition", "refused", "duplicate", "status"]
    state: ModelLaneWorkReducerState
    intents: tuple[ModelLaneWorkIntent, ...] = ()
    from_state: str
    refusal: str | None = None
    transition_name: str | None = None


__all__ = ["ModelLaneWorkReduceResult"]
