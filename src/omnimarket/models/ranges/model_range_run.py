# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One run of a range check's case set, with how it was sampled."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.ranges.model_range_sample import ModelRangeSample


class ModelRangeRun(BaseModel):
    """A run and the three facts that disqualify it as a range result.

    Section 2b, rule 4: no seed pinning, temperature forcing or
    retry-until-green to make a range pass. A pinned run measures the sampling
    budget, not the system, so the evaluator refuses any evaluation that
    contains one. The producer states these facts; the defaults describe an
    unpinned, unforced, single-attempt run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    sampling_seed: int | None = Field(
        default=None,
        description="The model sampling seed when one was pinned; None when it was not.",
    )
    temperature_forced: bool = Field(
        default=False,
        description="True when the run overrode the served model's sampling temperature.",
    )
    retried_until_pass: bool = Field(
        default=False,
        description="True when any case was re-run until it passed.",
    )
    samples: tuple[ModelRangeSample, ...] = Field(default=())
