# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Case set, request and scored result of node_reviewer_eval_effect (OMN-17427)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumReviewerEvalLabel(StrEnum):
    """What recorded history says the right verdict on a case is."""

    # The reviewer blocked and a later fix of the flagged code confirmed it.
    TRUE_CATCH = "true_catch"
    # The reviewer blocked and the finding was later shown wrong on the facts.
    OVERTURNED_BLOCK = "overturned_block"
    # A merged head with no revert since; the reviewer should let it through.
    KNOWN_CLEAN = "known_clean"


class EnumReviewerVerdict(StrEnum):
    """The hostile reviewer gate's verdict on one head."""

    BLOCKED = "blocked"
    PASSED = "passed"
    DEGRADED = "degraded"


class ModelReviewerEvalCase(BaseModel):
    """One labelled pull request head from recorded history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    repo: str = Field(pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
    pr: int = Field(gt=0)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    label: EnumReviewerEvalLabel
    recorded_verdict: EnumReviewerVerdict = Field(
        description="The gate's verdict on this head when it ran."
    )
    finding: str | None = Field(
        default=None, description="The blocking finding as recorded, for a block."
    )
    evidence: str = Field(
        min_length=1, description="The record that settles the label."
    )

    @property
    def should_block(self) -> bool:
        return self.label is EnumReviewerEvalLabel.TRUE_CATCH

    @model_validator(mode="after")
    def _block_cases_carry_their_block(self) -> ModelReviewerEvalCase:
        if self.label is not EnumReviewerEvalLabel.KNOWN_CLEAN:
            if self.recorded_verdict is not EnumReviewerVerdict.BLOCKED:
                raise ValueError(
                    f"{self.case_id}: a {self.label} case is defined by a recorded block"
                )
            if not self.finding:
                raise ValueError(f"{self.case_id}: a recorded block names its finding")
        elif self.recorded_verdict is not EnumReviewerVerdict.PASSED:
            raise ValueError(
                f"{self.case_id}: a known_clean head merged, so the gate passed it"
            )
        return self


class ModelReviewerEvalBaseline(BaseModel):
    """The score a reviewer change must not fall below."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recall: float = Field(ge=0.0, le=1.0)
    false_block_rate: float = Field(ge=0.0, le=1.0)


class ModelReviewerEvalCaseSet(BaseModel):
    """The eval set: labelled cases and the baseline score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["reviewer_eval_cases.v1"]
    baseline: ModelReviewerEvalBaseline
    cases: tuple[ModelReviewerEvalCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _scorable(self) -> ModelReviewerEvalCaseSet:
        ids = [case.case_id for case in self.cases]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"duplicate case_id: {', '.join(duplicated)}")
        if not any(case.should_block for case in self.cases):
            raise ValueError("the set holds no true catch, so recall is undefined")
        if all(case.should_block for case in self.cases):
            raise ValueError(
                "the set holds no clean case, so false blocks are unmeasured"
            )
        return self


class ModelReviewerEvalObservation(BaseModel):
    """The verdict the reviewer under test gave on one case's head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1)
    verdict: EnumReviewerVerdict


class ModelReviewerEvalRequest(BaseModel):
    """Score one reviewer configuration against the eval set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    reviewer_ref: str = Field(
        min_length=1, description="Which reviewer configuration produced the verdicts."
    )
    source: Literal["recorded", "live"] = Field(
        description="recorded: the verdicts the gate gave when it ran; live: a rerun."
    )
    case_set: ModelReviewerEvalCaseSet
    observations: tuple[ModelReviewerEvalObservation, ...]
    prior_case_set: ModelReviewerEvalCaseSet | None = Field(
        default=None,
        description="The base branch's set: a change may add cases and raise the "
        "baseline, never drop a case, flip a label or lower the baseline.",
    )


class ModelReviewerEvalCaseOutcome(BaseModel):
    """One case scored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    label: EnumReviewerEvalLabel
    should_block: bool
    observed: EnumReviewerVerdict
    correct: bool


class ModelReviewerEvalScored(BaseModel):
    """The reviewer's score on the eval set; the node's terminal event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    reviewer_ref: str
    source: Literal["recorded", "live"]
    true_catch_cases: int
    clean_cases: int
    caught: int
    false_blocks: int
    degraded: int
    recall: float
    false_block_rate: float
    baseline: ModelReviewerEvalBaseline
    regressed: bool
    reasons: tuple[str, ...]
    outcomes: tuple[ModelReviewerEvalCaseOutcome, ...]


__all__ = [
    "EnumReviewerEvalLabel",
    "EnumReviewerVerdict",
    "ModelReviewerEvalBaseline",
    "ModelReviewerEvalCase",
    "ModelReviewerEvalCaseOutcome",
    "ModelReviewerEvalCaseSet",
    "ModelReviewerEvalObservation",
    "ModelReviewerEvalRequest",
    "ModelReviewerEvalScored",
]
