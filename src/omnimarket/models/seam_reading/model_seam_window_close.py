# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one side of one seam observed in one window, which may be nothing."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading.enum_seam_side import EnumSeamSide
from omnimarket.models.seam_reading.model_seam_count import SEAM_ID_PATTERN
from omnimarket.models.seam_reading.model_seam_window import ModelSeamWindow


class ModelSeamWindowClose(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seam_id: str = Field(pattern=SEAM_ID_PATTERN)
    side: EnumSeamSide
    window: ModelSeamWindow
    reported_through: datetime
    crossing_ids: tuple[str, ...] = ()
