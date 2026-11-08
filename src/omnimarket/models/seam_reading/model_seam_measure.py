# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One measure a seam declares, with the ids one crossing carried for it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading.enum_seam_measure_kind import EnumSeamMeasureKind
from omnimarket.models.seam_reading.enum_seam_unit import EnumSeamUnit
from omnimarket.models.seam_reading.model_seam_id_disposition import (
    ModelSeamIdDisposition,
)


class ModelSeamMeasure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    measure_id: str = Field(min_length=1)
    unit: EnumSeamUnit
    kind: EnumSeamMeasureKind = EnumSeamMeasureKind.TRANSIT
    ids: tuple[ModelSeamIdDisposition, ...] = ()
