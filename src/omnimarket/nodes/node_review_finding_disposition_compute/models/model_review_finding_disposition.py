# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result models for the review finding disposition compute (OMN-17427)."""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.review_finding_disposition.model_review_finding_disposition_event import (
    FINDING_CLASS_PATTERN,
    REPO_PATTERN,
    ModelReviewFindingDispositionRecorded,
    ModelReviewPassRecorded,
)


class ModelReviewFindingDispositionInput(BaseModel):
    """Recorded answers and passes to fold, read at ``as_of``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    as_of: AwareDatetime = Field(description="The window ends here (inclusive).")
    window_days: int = Field(default=7, ge=1, le=366)
    recurrence_threshold: int = Field(
        default=3, ge=2, description="Distinct PRs that make a finding class recur."
    )
    dispositions: tuple[ModelReviewFindingDispositionRecorded, ...] = ()
    passes: tuple[ModelReviewPassRecorded, ...] = ()


class ModelRepoReviewMetrics(BaseModel):
    """One repository's review noise and convergence over the window.

    A share is ``None`` when its denominator is zero: no data is not a zero share.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(pattern=REPO_PATTERN)
    findings_answered: int = Field(ge=0)
    fixed: int = Field(ge=0)
    wrong: int = Field(ge=0)
    intentional: int = Field(ge=0)
    not_applicable: int = Field(ge=0)
    noise_share: float | None = Field(
        description="(wrong + not_applicable) / findings_answered.", ge=0, le=1
    )
    prs_passed: int = Field(ge=0)
    one_round_pass_share: float | None = Field(
        description="Share of passed PRs with at most one review-and-fix round.",
        ge=0,
        le=1,
    )
    five_plus_round_share: float | None = Field(
        description="Share of passed PRs that needed five or more rounds.", ge=0, le=1
    )
    mean_rounds_to_pass: float | None = Field(ge=0)


class ModelClassFixRequest(BaseModel):
    """One class-fix item to file: the same finding class recurred across PRs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_class: str = Field(pattern=FINDING_CLASS_PATTERN)
    dedupe_key: UUID = Field(
        description="One item per finding class: the filer skips a key it already filed."
    )
    pr_refs: tuple[str, ...] = Field(description="Sorted distinct owner/repo#number.")
    finding_ids: tuple[str, ...]
    fixed: int = Field(ge=0)
    wrong: int = Field(ge=0)
    intentional: int = Field(ge=0)
    not_applicable: int = Field(ge=0)
    window_start: AwareDatetime
    window_end: AwareDatetime


class ModelReviewFindingDispositionOutput(BaseModel):
    """Per-repo metrics and the class-fix items due at ``as_of``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    as_of: AwareDatetime
    window_days: int
    dispositions_counted: int = Field(ge=0)
    repos: tuple[ModelRepoReviewMetrics, ...]
    class_fix_requests: tuple[ModelClassFixRequest, ...]
