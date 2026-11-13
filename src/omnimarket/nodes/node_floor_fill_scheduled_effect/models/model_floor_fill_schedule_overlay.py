# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the floor fill scheduled effect reads: which fill overlay to run and on which profile."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelFloorFillScheduleOverlay(ModelScheduleDeclaration):
    """The overlay of the fill's own overlay document and the profile the tick runs on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fill_overlay_id: str = Field(min_length=1)
    profile: Literal["inmemory", "live"]


__all__: list[str] = [
    "ModelFloorFillScheduleOverlay",
]
