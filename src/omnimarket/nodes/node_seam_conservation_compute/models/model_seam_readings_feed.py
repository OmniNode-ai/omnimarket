# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the VS-17 caller: the recorded readings become the tick's typed field."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading import (
    ModelSeamReading,
    ModelSeamReadingsFacts,
    ModelSeamWindow,
)


class ModelSeamReadingsFeedRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: ModelSeamWindow
    finalized: bool
    publish_steps_ok: bool = True
    expected_seam_ids: tuple[str, ...]
    declared_seam_ids: tuple[str, ...] = ()
    recorded: tuple[ModelSeamReading, ...] = Field(
        default=(),
        description="Rows of the view_seam_readings exposure; a row of another window is not this window's reading.",
    )
    tick_id: str = Field(min_length=1)
    now: datetime


class ModelSeamReadingsFeedResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: ModelSeamReadingsFacts
