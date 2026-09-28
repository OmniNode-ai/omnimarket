# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One sampled case of one range run."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.ranges.enum_range_sample_outcome import (
    EnumRangeSampleOutcome,
)


class ModelRangeSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(..., min_length=1)
    outcome: EnumRangeSampleOutcome
