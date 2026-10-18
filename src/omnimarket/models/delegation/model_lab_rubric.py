# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-first scoring rubric a task-class contract declares (OMN-17427).

The operator's standing rule is that appropriate tasks go to the lab models
first. "Appropriate" needs a measure: a class is lab-passing when the lab model
reached the policy's adequacy bar on that class's held-out set, and a class the
lab has not been measured on keeps the default, which is lab first. Only a
recorded measurement below the bar releases a class from the rule.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumLabVerdict(StrEnum):
    """Whether the lab model met the adequacy bar on a task class."""

    PASS = "pass"
    FAIL = "fail"
    UNMEASURED = "unmeasured"


class ModelLabAdequacyBar(BaseModel):
    """The bar: ``adequate`` of ``of`` held-out answers judged adequate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adequate: int = Field(ge=1)
    of: int = Field(ge=1)

    @model_validator(mode="after")
    def _bar_fits_the_set(self) -> ModelLabAdequacyBar:
        if self.adequate > self.of:
            raise ValueError(f"bar {self.adequate} of {self.of} exceeds its own set")
        return self


class ModelLabFirstPolicy(BaseModel):
    """Which tier is the lab and what a class must score there to count as passing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lab_tier: str = Field(min_length=1)
    bar: ModelLabAdequacyBar
    rationale: str = Field(min_length=1)


class ModelLabMeasurement(BaseModel):
    """One scored run of the lab model on a class's held-out set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    adequate: int = Field(ge=0)
    of: int = Field(ge=1)
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def _score_fits_the_set(self) -> ModelLabMeasurement:
        if self.adequate > self.of:
            raise ValueError(f"score {self.adequate} of {self.of} exceeds its set")
        return self


class ModelLabRubric(BaseModel):
    """A task class's declared lab verdict and the measurement behind it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumLabVerdict
    measured: ModelLabMeasurement | None = None

    @model_validator(mode="after")
    def _measurement_matches_verdict(self) -> ModelLabRubric:
        unmeasured = self.verdict is EnumLabVerdict.UNMEASURED
        if unmeasured and self.measured is not None:
            raise ValueError("an unmeasured class carries no measurement")
        if not unmeasured and self.measured is None:
            raise ValueError(f"verdict {self.verdict.value} needs a measurement")
        return self

    def derived_verdict(self, bar: ModelLabAdequacyBar) -> EnumLabVerdict:
        """The verdict the measurement earns against ``bar``."""
        if self.measured is None:
            return EnumLabVerdict.UNMEASURED
        meets = self.measured.adequate * bar.of >= bar.adequate * self.measured.of
        return EnumLabVerdict.PASS if meets else EnumLabVerdict.FAIL


__all__ = [
    "EnumLabVerdict",
    "ModelLabAdequacyBar",
    "ModelLabFirstPolicy",
    "ModelLabMeasurement",
    "ModelLabRubric",
]
