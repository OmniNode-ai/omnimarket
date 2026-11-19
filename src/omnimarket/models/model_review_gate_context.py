# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review-gate context decision's models: the pull request subject, the declared bypass policy, and the verdict.

The decision is pure: a pull request's head passes only on a CLEAN review verdict recorded for that exact head, or
when it belongs to a declared bypass class. No clock and no I/O.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_landing_review import SHA_PATTERN, ModelLandingReviewFacts

REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"


class EnumReviewContextState(StrEnum):
    """The state the review-gate context check reports."""

    SUCCESS = "success"
    FAILURE = "failure"


class EnumReviewContextReason(StrEnum):
    """The named reason behind a review-gate context state."""

    CLEAN_ON_HEAD = "clean_on_head"
    BYPASS_CLASS = "bypass_class"
    REVIEW_MISSING = "review_missing"
    REVIEW_CHANGES_NEEDED = "review_changes_needed"
    REVIEW_SUPERSEDED = "review_superseded"


class ModelReviewBypassClass(BaseModel):
    """One declared class of pull request that needs no review verdict on its head.

        A class matches a pull request when the author is in ``author_logins`` (compared case-insensitively, exact) and
    every given pattern full-matches: ``head_ref_pattern`` against the head ref and ``title_pattern`` against the title.
    A class with no pattern at all is allowed (author-only) but only because its overlay entry declares it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    why: str = Field(min_length=1)
    author_logins: tuple[str, ...] = Field(min_length=1)
    head_ref_pattern: str | None = None
    title_pattern: str | None = None

    @model_validator(mode="after")
    def _patterns_compile(self) -> ModelReviewBypassClass:
        for pattern in (self.head_ref_pattern, self.title_pattern):
            if pattern is not None:
                re.compile(pattern)
        return self


class ModelReviewGateContextPolicy(BaseModel):
    """The declared policy of the review-gate context check: its name, bypass classes, and the five result titles."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_name: str = Field(min_length=1)
    bypass_classes: tuple[ModelReviewBypassClass, ...] = ()
    title_clean: str = Field(min_length=1)
    title_bypass: str = Field(min_length=1)
    title_missing: str = Field(min_length=1)
    title_changes_needed: str = Field(min_length=1)
    title_superseded: str = Field(min_length=1)

    @model_validator(mode="after")
    def _names_unique(self) -> ModelReviewGateContextPolicy:
        names = [c.name for c in self.bypass_classes]
        if len(names) != len(set(names)):
            raise ValueError("a bypass class name is declared twice")
        return self


class ModelReviewGatePrSubject(BaseModel):
    """The pull request the review-gate context decision is asked about."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(pattern=REPOSITORY_PATTERN)
    pr_number: int = Field(ge=1)
    head_sha: str = Field(pattern=SHA_PATTERN)
    author_login: str = Field(min_length=1)
    head_ref: str
    title: str
    review: ModelLandingReviewFacts | None = None


class ModelReviewGateContextRequest(BaseModel):
    """The input of the review-gate context decision: the subject and the declared policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: ModelReviewGatePrSubject
    policy: ModelReviewGateContextPolicy


class ModelReviewGateContextDecision(BaseModel):
    """The verdict of the review-gate context check for one pull request head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    pr_number: int
    head_sha: str
    check_name: str
    state: EnumReviewContextState
    reason: EnumReviewContextReason
    bypass_class: str | None
    title: str
    summary: str

    @model_validator(mode="after")
    def _shape(self) -> ModelReviewGateContextDecision:
        bypassed = self.reason is EnumReviewContextReason.BYPASS_CLASS
        if (self.bypass_class is not None) != bypassed:
            raise ValueError(
                "bypass_class is set exactly when the reason is bypass_class"
            )
        success = self.reason in (
            EnumReviewContextReason.CLEAN_ON_HEAD,
            EnumReviewContextReason.BYPASS_CLASS,
        )
        if (self.state is EnumReviewContextState.SUCCESS) != success:
            raise ValueError(
                "state is success exactly on clean_on_head or bypass_class"
            )
        return self


__all__ = [
    "REPOSITORY_PATTERN",
    "EnumReviewContextReason",
    "EnumReviewContextState",
    "ModelReviewBypassClass",
    "ModelReviewGateContextDecision",
    "ModelReviewGateContextPolicy",
    "ModelReviewGateContextRequest",
    "ModelReviewGatePrSubject",
]
