# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State and events of node_view_seam_reading_reducer: the folded record of the seam family."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading import (
    EnumSeamCrossingState,
    ModelSeamCount,
    ModelSeamReading,
    ModelSeamWindowClose,
)


class ModelViewSeamState(BaseModel):
    """One row per (seam, side, crossing), one close per (seam, side, window), one reading per (seam, window)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    counts: dict[str, ModelSeamCount] = Field(default_factory=dict)
    closes: dict[str, ModelSeamWindowClose] = Field(default_factory=dict)
    readings: dict[str, ModelSeamReading] = Field(default_factory=dict)


class ModelViewSeamCountEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["count"] = "count"
    count: ModelSeamCount


class ModelViewSeamCloseEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["close"] = "close"
    close: ModelSeamWindowClose


class ModelViewSeamReadingEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["reading"] = "reading"
    reading: ModelSeamReading


ModelViewSeamEvent = Annotated[
    ModelViewSeamCountEvent | ModelViewSeamCloseEvent | ModelViewSeamReadingEvent,
    Field(discriminator="kind"),
]


class ModelViewSeamFoldRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelViewSeamState = Field(default_factory=ModelViewSeamState)
    event: ModelViewSeamEvent


class ModelViewSeamReadingRow(BaseModel):
    """One row of the view_seam_readings exposure; the reducer is its only writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seam_id: str
    window_start: datetime
    window_end: datetime
    state: EnumSeamCrossingState
    crossings: int = Field(ge=0)
    mismatched: int = Field(ge=0)
    unreadable: int = Field(ge=0)
    examples: tuple[str, ...] = ()
    read_at: datetime


class ModelViewSeamFoldResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelViewSeamState
    row: ModelViewSeamReadingRow | None = Field(
        default=None,
        description="The exposure row a reading event wrote; None for a count or a close.",
    )
