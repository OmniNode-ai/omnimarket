# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of node_seam_conservation_compute: the reading, and the residuals and counts behind it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading import EnumSeamCrossingState, ModelSeamReading


class ModelSeamResidual(BaseModel):
    """The two residuals of one measure, both of which must be empty; dropped is computed, never reported."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    measure_id: str
    crossing_id: str | None = None
    transit: tuple[str, ...] = ()
    disposition: tuple[str, ...] = ()
    unused: tuple[str, ...] = Field(
        default=(),
        description="Coverage inventory the receiver does not use: reported, not a fault.",
    )


class ModelSeamDerivedCounts(BaseModel):
    """Counts derived from the id sets, stored on the reading for the dashboard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    out: int = Field(ge=0)
    received: int = Field(ge=0)
    accepted: int = Field(ge=0)
    stale: int = Field(ge=0)
    blocked: int = Field(ge=0)
    refused: int = Field(ge=0)


class ModelSeamConservationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: EnumSeamCrossingState
    reason: str
    reading: ModelSeamReading
    residuals: tuple[ModelSeamResidual, ...] = ()
    counts: ModelSeamDerivedCounts
    max_latency_seconds: float | None = None
