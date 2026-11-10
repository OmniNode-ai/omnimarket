# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay keys of the lane process effect: floor_fill.output_dir, with no default."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFloorFillProcessKeys(BaseModel):
    """The floor_fill keys the effect reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_dir: str = Field(min_length=1)


class ModelLaneProcessOverlay(BaseModel):
    """The effect's overlay; a missing key refuses naming it (floor_fill.output_dir)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    floor_fill: ModelFloorFillProcessKeys
