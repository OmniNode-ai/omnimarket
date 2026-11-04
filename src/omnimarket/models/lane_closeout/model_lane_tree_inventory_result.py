# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of the lane tree inventory."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lane_closeout.model_lane_tree_line import ModelLaneTreeLine


class ModelLaneTreeInventoryResult(BaseModel):
    """One ``ModelLaneTreeLine`` per tree, in path order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    trees: tuple[ModelLaneTreeLine, ...] = Field(default=())
    lines: tuple[str, ...] = Field(default=())
