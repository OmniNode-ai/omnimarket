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


class ModelLandingReviewVerdictRow(BaseModel):
    """One review verdict recorded for one head, at the time it was given."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str = Field(..., pattern=SHA_PATTERN)
    verdict: EnumReviewVerdict
    at: datetime


class ModelLandingReviewFacts(BaseModel):
    """Optional on the PR's facts. new_work is true when the PR's ticket or contract marks it as new work. A PR whose
    new_work is true merges only on a CLEAN verdict for its exact head.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    new_work: bool
    verdicts: tuple[ModelLandingReviewVerdictRow, ...] = ()


class ModelReviewGateReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReviewGateVerdict
    detail: str


__all__ = [
    "EnumReviewGateVerdict",
    "EnumReviewVerdict",
    "ModelLandingReviewFacts",
    "ModelLandingReviewVerdictRow",
    "ModelReviewGateReading",
]
