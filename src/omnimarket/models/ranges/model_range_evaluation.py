# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The result of evaluating one range line over a set of runs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.ranges.enum_range_verdict import EnumRangeVerdict


class ModelRangeEvaluation(BaseModel):
    """The verdict plus everything it was computed from.

    ``lower_bound`` is the number the floor is compared against: the more
    conservative of the one-sided bootstrap bound and the exact bound, both at
    ``confidence`` and both reported. ``interval_low``/``interval_high`` are
    the two-sided bootstrap interval at the same confidence, for the report.
    The statistics are None when the evaluation was refused before any rate
    could be computed (no samples).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    verdict: EnumRangeVerdict
    reasons: tuple[str, ...] = Field(default=())
    floor: float
    confidence: float
    required_n: int = Field(..., description="The power-analysed n for this line.")
    declared_n: int
    observed_n: int
    passes: int
    failures: int
    incomplete: int
    point_estimate: float | None = None
    lower_bound: float | None = Field(
        default=None,
        description="The decision bound: min(bootstrap_lower_bound, exact_lower_bound).",
    )
    bootstrap_lower_bound: float | None = None
    exact_lower_bound: float | None = Field(
        default=None,
        description="One-sided Clopper-Pearson bound at the same confidence.",
    )
    interval_low: float | None = None
    interval_high: float | None = None
    bootstrap_resamples: int
