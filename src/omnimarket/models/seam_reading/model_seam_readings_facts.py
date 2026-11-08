# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The finalized window's seam readings, as the throughput tick consumes them (VS-17)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading.model_seam_reading import ModelSeamReading
from omnimarket.models.seam_reading.model_seam_window import ModelSeamWindow


class ModelSeamReadingsFacts(BaseModel):
    """What the caller read from the `view_seam_readings` exposure for one window.

    `expected_seam_ids` comes from the seam graph, one per declared seam; `readings` are the
    recorded readings, and a seam with no reading in the window is absent from them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window: ModelSeamWindow
    finalized: bool
    publish_steps_ok: bool = Field(
        default=True,
        description="Whether every publish step of the window reported ok; it never decides a seam's reading.",
    )
    expected_seam_ids: tuple[str, ...]
    readings: tuple[ModelSeamReading, ...] = ()
    declared_seam_ids: tuple[str, ...] = Field(
        default=(),
        description="Seams whose status in the graph is still declared, named by one NOTE line.",
    )


class ModelSeamReadingsVerdict(BaseModel):
    """What the tick consumed and raised for one window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window: ModelSeamWindow
    expected_ids: tuple[str, ...]
    consumed_ids: tuple[str, ...]
    missing_ids: tuple[str, ...] = ()
    unknown_ids: tuple[str, ...] = ()
    declared_ids: tuple[str, ...] = ()
