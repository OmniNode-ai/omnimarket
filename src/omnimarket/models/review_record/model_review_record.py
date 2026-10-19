# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review record: one per arm of a review invocation (OMN-21188).

An invocation that runs several routes gets one ``invocation_id``; each route
writes its own :class:`ModelReviewRecord`, so a finding belongs to exactly one
arm. A value the producer could not establish is stated, never filled in: the
served model is ``unknown``, unread settings and token counts are ``None``, and a
target without a snapshot says why.
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_review_execution_status import EnumReviewExecutionStatus
from omnimarket.models.model_review_finding import EnumReviewVerdict
from omnimarket.models.review_record.model_recorded_finding import (
    ModelRecordedFinding,
)

UNKNOWN = "unknown"
NO_RUBRIC = "none"

SettingValue = str | int | float | bool | None


class ModelReviewServed(BaseModel):
    """What the inference result reports it actually served."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(default=UNKNOWN, min_length=1)
    settings: dict[str, SettingValue] | None = Field(
        default=None, description="None when the inference result reports none."
    )
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)


class ModelReviewArm(BaseModel):
    """The route an arm asked for, and what it was served."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: str = Field(min_length=1)
    requested_settings: dict[str, SettingValue] = Field(default_factory=dict)
    served: ModelReviewServed = Field(default_factory=ModelReviewServed)


class ModelReviewTarget(BaseModel):
    """What was reviewed, from one immutable snapshot or stated unavailable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    pr_number: int | None = Field(default=None, ge=1)
    snapshot_available: bool = True
    head_sha: str | None = None
    base_sha: str | None = None
    diff_digest: str | None = None
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def _snapshot_is_whole_or_unavailable(self) -> Self:
        shas = (self.head_sha, self.base_sha, self.diff_digest)
        if self.snapshot_available:
            if not all(shas) or self.unavailable_reason is not None:
                raise ValueError(
                    "an available snapshot carries head_sha, base_sha and "
                    "diff_digest together and no unavailable_reason"
                )
        elif self.unavailable_reason is None or any(shas):
            raise ValueError(
                "an unavailable snapshot states unavailable_reason and carries "
                "no head_sha, base_sha or diff_digest"
            )
        return self


class ModelReviewPrincipal(BaseModel):
    """The reviewing principal and lane, with where the identity came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    provenance: str = Field(min_length=1)


class ModelReviewCost(BaseModel):
    """Wall time and tokens where known. No price."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    wall_time_ms: int = Field(ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)


class ModelReviewRecord(BaseModel):
    """One arm's review of one target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_id: UUID
    invocation_id: UUID
    arm: ModelReviewArm
    rubric_hash: str = Field(default=NO_RUBRIC, min_length=1)
    prompt_template_id: str = Field(min_length=1)
    target: ModelReviewTarget
    started_at: AwareDatetime
    finished_at: AwareDatetime
    verdict: EnumReviewVerdict | None = Field(
        default=None, description="None when the arm produced no verdict."
    )
    findings: tuple[ModelRecordedFinding, ...] = ()
    cost: ModelReviewCost
    source_node: str = Field(min_length=1)
    execution_status: EnumReviewExecutionStatus
    status_reason: str | None = None
    principal: ModelReviewPrincipal

    @model_validator(mode="after")
    def _ordered_and_explained(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        completed = self.execution_status is EnumReviewExecutionStatus.COMPLETED
        if not completed and not self.status_reason:
            raise ValueError("a partial or failed record states its reason")
        return self


__all__: list[str] = [
    "NO_RUBRIC",
    "UNKNOWN",
    "ModelReviewArm",
    "ModelReviewCost",
    "ModelReviewPrincipal",
    "ModelReviewRecord",
    "ModelReviewServed",
    "ModelReviewTarget",
]
