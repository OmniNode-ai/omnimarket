# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The result of comparing two rungs on the same prompts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.ranges.enum_comparison_verdict import EnumComparisonVerdict


class ModelComparisonResult(BaseModel):
    """The verdict and everything it was computed from.

    ``difference`` is rung B's pass rate minus rung A's over the same prompts.
    ``interval_low``/``interval_high`` are the two-sided paired bootstrap
    interval of that difference at ``confidence``. The verdict is decided by the
    exact two-sided McNemar test on the discordant pairs (``p_value``), which
    never exceeds its nominal false-difference rate. An incomplete answer counts
    as a failure in its arm's rate and is reported by count.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    comparison_id: str
    verdict: EnumComparisonVerdict
    reasons: tuple[str, ...] = Field(default=())
    confidence: float
    margin: float
    required_n: int
    declared_n: int
    observed_n: int
    passes_a: int
    passes_b: int
    incomplete_a: int
    incomplete_b: int
    a_only: int = Field(..., description="Pairs where A passed and B did not.")
    b_only: int = Field(..., description="Pairs where B passed and A did not.")
    rate_a: float | None = None
    rate_b: float | None = None
    difference: float | None = None
    interval_low: float | None = None
    interval_high: float | None = None
    p_value: float | None = None
    bootstrap_resamples: int
