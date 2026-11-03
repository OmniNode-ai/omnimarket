# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer's input: the stored row and one observation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_floor_fill_reducer.models.model_floor_fill_observation import (
    ModelFloorFillObservation,
)
from omnimarket.nodes.node_floor_fill_reducer.models.model_floor_fill_state import (
    ModelFloorFillState,
)


class ModelFloorFillReduceInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelFloorFillState = Field(default_factory=ModelFloorFillState)
    observation: ModelFloorFillObservation


__all__: list[str] = ["ModelFloorFillReduceInput"]
