# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review gate's commit status as the landing decision and pr-handoff read it.

The review gate posts one ``fable-review-gate`` commit status per head. A status belongs to the commit it was
posted on, so a status on an older head says nothing about the head being merged or handed off. These models
carry the statuses a caller read for a pull request (the current head's and any earlier head's), and the reading
names which of four cases a head is in: it has a success status, it has none, its newest is not a success, or only
another head has one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_repo_write_io import COMMIT_STATUS_CONTEXT

SHA_PATTERN = r"^[0-9a-f]{40}$"
#: The one status context the gate is posted under.
REVIEW_STATUS_CONTEXT = COMMIT_STATUS_CONTEXT


class EnumReviewStatusVerdict(StrEnum):
    """What a head's review-gate commit statuses say."""

    PASS = "pass"
    MISSING = "missing"
    FAILING = "failing"
    OTHER_HEAD = "other_head"
    UNREAD = "unread"


class ModelReviewStatusRow(BaseModel):
    """One commit status read for one head: the commit it was posted on, its context and state, and when."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str = Field(..., pattern=SHA_PATTERN)
    context: str = Field(..., min_length=1)
    state: str = Field(..., min_length=1)
    at: datetime


class ModelReviewStatusReading(BaseModel):
    """The verdict of a head's review-gate statuses and the sentence that says which case it is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReviewStatusVerdict
    detail: str


__all__ = [
    "REVIEW_STATUS_CONTEXT",
    "EnumReviewStatusVerdict",
    "ModelReviewStatusReading",
    "ModelReviewStatusRow",
]
