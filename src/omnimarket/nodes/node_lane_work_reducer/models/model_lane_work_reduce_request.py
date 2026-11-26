# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request to the reducer node (OMN-26706)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_config import (
    ModelLaneWorkReducerConfig,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_state import (
    ModelLaneWorkReducerState,
)


class ModelLaneWorkReduceRequest(BaseModel):
    """State and one event (typed or the classifier's), at a given fixed time."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    state: ModelLaneWorkReducerState
    event: Any
    now: datetime | None = None
    config: ModelLaneWorkReducerConfig = Field(
        default_factory=ModelLaneWorkReducerConfig
    )


__all__ = ["ModelLaneWorkReduceRequest"]
