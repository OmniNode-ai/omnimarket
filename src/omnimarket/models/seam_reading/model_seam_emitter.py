# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and typed INSUFFICIENT result of an emitter node whose step has not built it yet."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading.enum_seam_side import EnumSeamSide
from omnimarket.models.seam_reading.model_seam_count import SEAM_ID_PATTERN


class ModelSeamEmitterRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seam_id: str = Field(pattern=SEAM_ID_PATTERN)
    side: EnumSeamSide


class ModelSeamEmitterInsufficient(BaseModel):
    """The emitter is declared and publishes nothing yet; the seam reads unreadable until it does."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["INSUFFICIENT"] = "INSUFFICIENT"
    emitter: str
    seam_id: str
    side: EnumSeamSide
    reason: str
