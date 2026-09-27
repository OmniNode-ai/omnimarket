# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One prompt answered by both rungs, each answer graded."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.ranges.enum_range_sample_outcome import (
    EnumRangeSampleOutcome,
)


class ModelComparisonPair(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(..., min_length=1)
    outcome_a: EnumRangeSampleOutcome
    outcome_b: EnumRangeSampleOutcome
