# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review gate's models: the review verdicts a PR carries, and the reading the landing decision makes of them.

``read_review_gate`` is what the landing decision asks before it merges a new-work head. Only a verdict on the exact
head counts: verdicts recorded for other heads are evidence of nothing about this one, and a head with only such
verdicts reads ``superseded``. No clock and no I/O.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_review_status import ModelReviewStatusRow

SHA_PATTERN = r"^[0-9a-f]{40}$"


class EnumReviewVerdict(StrEnum):
    """What a recorded review verdict says of one head."""

    CLEAN = "clean"
    CHANGES_NEEDED = "changes_needed"


class EnumReviewGateVerdict(StrEnum):
    """What the decision reads off a head's review verdicts."""

    PASS = "pass"
    MISSING = "missing"
    CHANGES_NEEDED = "changes_needed"
    SUPERSEDED = "superseded"
    UNREAD = "unread"


class EnumReviewFindingClass(StrEnum):
    """What a review finding does to a merge: a blocking one holds the head, an advisory one never does."""

    BLOCKING = "blocking"
    ADVISORY = "advisory"


class ModelLandingReviewFinding(BaseModel):
    """One finding a review verdict lists. Only what the classification reads is typed.

    ``failing_check`` names a check that fails. ``file``, ``line`` and ``defect`` together cite a code line with a
    defect. A finding with neither (for example a request for ``installed=`` or ``observed=`` proof) is advisory.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(..., min_length=1)
    failing_check: str | None = Field(default=None, min_length=1)
    file: str | None = Field(default=None, min_length=1)
    line: int | None = Field(default=None, ge=1)
    defect: str | None = Field(default=None, min_length=1)


class ModelLandingReviewVerdictRow(BaseModel):
    """One review verdict recorded for one head, at the time it was given, with the findings it listed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str = Field(..., pattern=SHA_PATTERN)
    verdict: EnumReviewVerdict
    at: datetime
    findings: tuple[ModelLandingReviewFinding, ...] = ()


class ModelLandingReviewFacts(BaseModel):
    """Optional on the PR's facts. new_work is true when the PR's ticket or contract marks it as new work. A PR whose
    new_work is true merges only on a CLEAN verdict for its exact head.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    new_work: bool
    verdicts: tuple[ModelLandingReviewVerdictRow, ...] = ()
    statuses: tuple[ModelReviewStatusRow, ...] | None = Field(
        default=None,
        description=(
            "The commit statuses read for the PR (the current head's and any earlier head's). A new-work PR "
            "merges only with a success fable-review-gate status on its exact head; a status on an older head "
            "does not count. None: the statuses were not read."
        ),
    )


class ModelReviewGateReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReviewGateVerdict
    detail: str
    advisory_findings: tuple[ModelLandingReviewFinding, ...] = ()


__all__ = [
    "EnumReviewFindingClass",
    "EnumReviewGateVerdict",
    "EnumReviewVerdict",
    "ModelLandingReviewFacts",
    "ModelLandingReviewFinding",
    "ModelLandingReviewVerdictRow",
    "ModelReviewGateReading",
]
