# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer's output: the next row, the trigger taken or the drop reason, and the intents."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.floor_fill.enum_floor_fill_drop_reason import (
    EnumFloorFillDropReason,
)
from omnimarket.models.floor_fill.enum_floor_fill_trigger import (
    EnumFloorFillTrigger,
)
from omnimarket.models.floor_fill.model_floor_fill_intent import (
    ModelFloorFillIntent,
)
from omnimarket.models.floor_fill.model_floor_fill_state import (
    ModelFloorFillState,
)


class ModelFloorFillReduceOutput(BaseModel):
    """Exactly one of ``trigger`` and ``dropped_reason`` is set; a drop returns the row as it was."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelFloorFillState
    trigger: EnumFloorFillTrigger | None = None
    dropped_reason: EnumFloorFillDropReason | None = None
    intents: tuple[ModelFloorFillIntent, ...] = ()

    @model_validator(mode="after")
    def _a_trigger_or_a_reason(self) -> Self:
        if (self.trigger is None) == (self.dropped_reason is None):
            raise ValueError("exactly one of trigger and dropped_reason is set")
        if self.dropped_reason is not None and self.intents:
            raise ValueError("a dropped observation returns no intent")
        return self


__all__: list[str] = ["ModelFloorFillReduceOutput"]
