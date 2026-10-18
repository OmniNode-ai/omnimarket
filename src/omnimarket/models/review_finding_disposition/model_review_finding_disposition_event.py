# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed answers to hostile-reviewer findings, and the review pass signal (OMN-17427).

A fix worker answers each finding the reviewer raised on a PR as ``fixed``,
``wrong``, ``intentional`` or ``not_applicable``. The worker writes one
:class:`ModelReviewFindingDispositionEntry` per finding into its result; the
landing controller publishes each as a :class:`ModelReviewFindingDispositionRecorded`.
When the reviewer passes a PR, :class:`ModelReviewPassRecorded` records how many
review-and-fix rounds it took.

A round is one review followed by one fix push. ``rounds_to_pass`` is the number
of such rounds before the pass: 0 means the first review passed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Self
from uuid import UUID, uuid5

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from omnimarket.enums.enum_review_finding_disposition import (
    EnumReviewFindingDisposition,
)
from omnimarket.events.topics import (
    REVIEW_FINDING_DISPOSITION_RECORDED_TOPIC_V1,
    REVIEW_PASS_RECORDED_TOPIC_V1,
)

DISPOSITION_TOPIC = REVIEW_FINDING_DISPOSITION_RECORDED_TOPIC_V1
PASS_TOPIC = REVIEW_PASS_RECORDED_TOPIC_V1

REPO_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
FINDING_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}$"
FINDING_CLASS_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
WORKER_LANE_PATTERN = r"^[^\s|]+$"

DISPOSITION_ID_NAMESPACE = UUID("5e0f7c0e-6c1b-5d52-8d3e-3b8f1c6a9a21")
PASS_ID_NAMESPACE = UUID("a3c9d1f4-2b7e-5a60-9c14-7e5d2f8b0c33")


def disposition_id_for(
    repo: str, pr_number: int, review_round: int, finding_id: str
) -> UUID:
    """Stable key of one answer: a redelivery keeps it, a changed answer shares it."""
    return uuid5(
        DISPOSITION_ID_NAMESPACE, f"{repo}#{pr_number}|{review_round}|{finding_id}"
    )


def pass_id_for(repo: str, pr_number: int) -> UUID:
    """Stable key of a PR's pass: a PR passes once."""
    return uuid5(PASS_ID_NAMESPACE, f"{repo}#{pr_number}")


class ModelReviewFindingDispositionEntry(BaseModel):
    """What a fix worker writes for one finding it answered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(pattern=FINDING_ID_PATTERN)
    finding_class: str = Field(
        pattern=FINDING_CLASS_PATTERN,
        description="The reviewer's rule or class slug; the same slug on every PR.",
    )
    disposition: EnumReviewFindingDisposition


class ModelReviewFindingDispositionRecorded(BaseModel):
    """One finding's answer on one PR in one review round."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(pattern=REPO_PATTERN)
    pr_number: int = Field(ge=1)
    review_round: int = Field(
        ge=1, description="The round whose findings this answers."
    )
    finding_id: str = Field(pattern=FINDING_ID_PATTERN)
    finding_class: str = Field(pattern=FINDING_CLASS_PATTERN)
    disposition: EnumReviewFindingDisposition
    worker_lane: str = Field(pattern=WORKER_LANE_PATTERN)
    recorded_at: AwareDatetime
    disposition_id: UUID

    @model_validator(mode="after")
    def _validate_disposition_id(self) -> Self:
        expected = disposition_id_for(
            self.repo, self.pr_number, self.review_round, self.finding_id
        )
        if self.disposition_id != expected:
            raise ValueError(
                "disposition_id does not match repo, pr_number, review_round and finding_id"
            )
        return self

    @classmethod
    def build(
        cls,
        *,
        repo: str,
        pr_number: int,
        review_round: int,
        entry: ModelReviewFindingDispositionEntry,
        worker_lane: str,
        recorded_at: datetime,
    ) -> Self:
        """Build the event for a worker's entry with its deterministic key."""
        return cls(
            repo=repo,
            pr_number=pr_number,
            review_round=review_round,
            finding_id=entry.finding_id,
            finding_class=entry.finding_class,
            disposition=entry.disposition,
            worker_lane=worker_lane,
            recorded_at=recorded_at.astimezone(UTC),
            disposition_id=disposition_id_for(
                repo, pr_number, review_round, entry.finding_id
            ),
        )


class ModelReviewPassRecorded(BaseModel):
    """The hostile reviewer passed this PR after ``rounds_to_pass`` fix rounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(pattern=REPO_PATTERN)
    pr_number: int = Field(ge=1)
    rounds_to_pass: int = Field(ge=0)
    recorded_at: AwareDatetime
    pass_id: UUID

    @model_validator(mode="after")
    def _validate_pass_id(self) -> Self:
        if self.pass_id != pass_id_for(self.repo, self.pr_number):
            raise ValueError("pass_id does not match repo and pr_number")
        return self

    @classmethod
    def build(
        cls,
        *,
        repo: str,
        pr_number: int,
        rounds_to_pass: int,
        recorded_at: datetime,
    ) -> Self:
        """Build the event with its deterministic key."""
        return cls(
            repo=repo,
            pr_number=pr_number,
            rounds_to_pass=rounds_to_pass,
            recorded_at=recorded_at.astimezone(UTC),
            pass_id=pass_id_for(repo, pr_number),
        )


__all__ = [
    "DISPOSITION_ID_NAMESPACE",
    "DISPOSITION_TOPIC",
    "PASS_ID_NAMESPACE",
    "PASS_TOPIC",
    "ModelReviewFindingDispositionEntry",
    "ModelReviewFindingDispositionRecorded",
    "ModelReviewPassRecorded",
    "disposition_id_for",
    "pass_id_for",
]
