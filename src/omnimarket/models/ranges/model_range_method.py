# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four predeclared parts of a range line's method (unified plan section 2b)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.ranges.enum_incomplete_run_treatment import (
    EnumIncompleteRunTreatment,
)


class ModelRangeMethod(BaseModel):
    """n, confidence, power at a stated margin, and the incomplete-run treatment.

    Every field is required. A numeric line missing any of the four is not a
    range line: it is labelled a policy threshold or withdrawn, never evaluated.
    ``sample_size`` must be at least the n the power analysis gives for the
    line's floor, margin, confidence and power; the evaluator and the register
    check both recompute that n and refuse a line declared below it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sample_size: int = Field(
        ...,
        ge=1,
        description="n, declared before the runs and sized by the power analysis.",
    )
    confidence: float = Field(
        ...,
        gt=0.5,
        lt=1.0,
        description="One-sided confidence level of the lower bound (e.g. 0.95).",
    )
    power: float = Field(
        ...,
        gt=0.0,
        lt=1.0,
        description="Probability of meeting the line when the true rate is floor + margin.",
    )
    margin: float = Field(
        ...,
        gt=0.0,
        lt=1.0,
        description="The minimum detectable margin the power is stated at.",
    )
    incomplete_run_treatment: EnumIncompleteRunTreatment = Field(
        ...,
        description="What a missing, cancelled or incomplete run, or an unscored sample, counts as.",
    )
