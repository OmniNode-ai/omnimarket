# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One crossing of a seam, reported by one side, never a running total."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading.enum_seam_side import EnumSeamSide
from omnimarket.models.seam_reading.model_seam_artifact import ModelSeamArtifact
from omnimarket.models.seam_reading.model_seam_measure import ModelSeamMeasure

SEAM_ID_PATTERN = r"^VS-(0[1-9]|1[0-7])$"


class ModelSeamCount(BaseModel):
    """Keyed by (seam_id, side, crossing_id); a replay of the same key replaces its row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seam_id: str = Field(pattern=SEAM_ID_PATTERN)
    side: EnumSeamSide
    crossing_id: str = Field(min_length=1)
    origin_tick_id: str = Field(min_length=1)
    measures: tuple[ModelSeamMeasure, ...] = ()
    artifacts: tuple[ModelSeamArtifact, ...] = ()

    @property
    def key(self) -> tuple[str, EnumSeamSide, str]:
        return (self.seam_id, self.side, self.crossing_id)
