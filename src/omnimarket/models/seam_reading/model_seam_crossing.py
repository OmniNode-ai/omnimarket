# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One crossing of a seam, classified by a seam reader."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.seam_reading.enum_seam_crossing_state import (
    EnumSeamCrossingState,
)


class ModelSeamCrossing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    state: EnumSeamCrossingState
    reason: str = ""
