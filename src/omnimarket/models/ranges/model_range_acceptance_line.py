# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A range check's acceptance line, written before the runs (section 2b, rule 1)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.ranges.model_range_method import ModelRangeMethod


class ModelRangeAcceptanceLine(BaseModel):
    """The case set, n, floor, margin, window, confidence, power and treatment.

    The statistic is the pass rate: the share of samples whose case met its own
    criterion. The line is met when the one-sided lower bootstrap bound of that
    rate, at ``method.confidence``, clears ``floor``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str = Field(..., min_length=1)
    statistic: Literal["pass_rate"] = Field(default="pass_rate")
    case_set: str = Field(
        ..., min_length=1, description="What population the samples are drawn from."
    )
    declared_case_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "When set, the cases every run must cover. A declared case absent "
            "from a run counts as an INCOMPLETE sample of that run: the declared "
            "set, not the observed one, decides completeness."
        ),
    )
    floor: float = Field(..., gt=0.0, lt=1.0)
    window: str = Field(
        ..., min_length=1, description="The window the samples are drawn over."
    )
    method: ModelRangeMethod

    @model_validator(mode="after")
    def _alternative_is_a_rate(self) -> Self:
        if self.floor + self.method.margin > 1.0:
            raise ValueError(
                f"floor {self.floor} + margin {self.method.margin} exceeds 1.0: "
                "the power is stated at floor + margin, which must be a rate"
            )
        if len(set(self.declared_case_ids)) != len(self.declared_case_ids):
            raise ValueError("declared_case_ids carries a duplicate case id")
        return self
