# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The predeclared method of a paired rung comparison (plan row G3, section 2b)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.ranges.enum_incomplete_run_treatment import (
    EnumIncompleteRunTreatment,
)


class ModelComparisonMethod(BaseModel):
    """n prompts, the confidence, the power at a stated margin, and the treatment.

    ``margin`` is the smallest difference in pass rate between the two rungs the
    comparison must detect with probability ``power``. ``sample_size`` must be at
    least the n the power analysis gives; the comparison recomputes that n and
    refuses a method declared below it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sample_size: int = Field(
        ..., ge=1, description="Prompts, sized by the power analysis."
    )
    confidence: float = Field(
        ..., gt=0.5, lt=1.0, description="Two-sided confidence level."
    )
    power: float = Field(..., gt=0.0, lt=1.0)
    margin: float = Field(
        ..., gt=0.0, lt=1.0, description="Minimum detectable difference."
    )
    incomplete_run_treatment: EnumIncompleteRunTreatment
