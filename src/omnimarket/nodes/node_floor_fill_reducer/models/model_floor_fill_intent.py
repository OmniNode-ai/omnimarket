# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one intent the reducer returns: start a lane for a reserved key."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_floor_fill_reducer.models.model_floor_fill_state import (
    FloorKey,
)


class ModelFloorFillIntent(BaseModel):
    """``StartLane``: returned only by a reserve, once per attempt, for an entry holding a ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["StartLane"] = "StartLane"
    key: FloorKey
    lane: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    attempt: int = Field(ge=1)


__all__: list[str] = ["ModelFloorFillIntent"]
