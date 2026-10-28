# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of node_seam_reading_compute (OMN-21459)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading import ModelSeamReading


class ModelSeamReadingRequest(BaseModel):
    """Readings and ``now``; the seam nodes and thresholds come from the orchestrator contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    readings: tuple[ModelSeamReading, ...] = ()
    now: datetime


class ModelSeamReadConfig(BaseModel):
    """The ``config.seam_read`` block of the orchestrator contract."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    default_minimum_crossings: int = Field(ge=0)
    consecutive_clean_windows: int = Field(ge=1)
    window_hours: int = Field(ge=1)
    freshness_ticks: int = Field(ge=0)


class ModelSeamNode(BaseModel):
    """One seam node of the factory line graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seam_id: str
    node_id: str
    expected_crossings: int = Field(ge=0)
    bound_minutes: int | None = None
    shape: str | None = None
    gap: str | None = None


class ModelSeamReadingResult(BaseModel):
    """The rendered lines, in line order, and the config they were rendered under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: tuple[str, ...]
    config: ModelSeamReadConfig
    rendered_at: datetime
