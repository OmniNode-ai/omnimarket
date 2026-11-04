# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of the lane-end run."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lane_closeout.model_lane_closeout_result import (
    ModelLaneCloseoutResult,
)
from omnimarket.models.lane_closeout.model_lane_tree_inventory_result import (
    ModelLaneTreeInventoryResult,
)


class ModelLaneCloseoutRunResult(BaseModel):
    """The inventory, then the closeout of each tree, and one summary for the TERMINAL text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    lane: str
    inventory: ModelLaneTreeInventoryResult
    closeouts: tuple[ModelLaneCloseoutResult, ...] = Field(default=())
    summary: str
