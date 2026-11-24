# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, result and port models of the lane push node (OMN-26666).

The request has no field that holds a credential and forbids extra fields, so a
lane cannot hand one in, and cannot ask for a force push either.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# The remote head of a branch that does not exist yet.
NULL_HEAD: Final[str] = "0" * 40

_SHA = r"^[0-9a-f]{40}$"
_REPOSITORY = r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$"
_BRANCH = r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}$"


class EnumLanePushOutcome(StrEnum):
    PUSHED = "pushed"
    REFUSED = "refused"
    MOVED = "moved"


class EnumLanePushRefusal(StrEnum):
    NOT_A_TEST_REPOSITORY = "not_a_test_repository"
    PROTECTED_BRANCH = "protected_branch"
    CHECK_FAILED = "check_failed"
    PUSH_REJECTED = "push_rejected"


class ModelLanePushSource(BaseModel):
    """Where the commits are: a bundle the lane stored, or a ref it stored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["bundle", "stored_ref"]
    location: str = Field(min_length=1)
    head_sha: str = Field(pattern=_SHA)


class ModelLanePushRequest(BaseModel):
    """One lane's hand-over for one branch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    lane: str = Field(min_length=1)
    repository: str = Field(pattern=_REPOSITORY)
    branch: str = Field(pattern=_BRANCH)
    expected_remote_head: str = Field(pattern=_SHA)
    source: ModelLanePushSource


class ModelLanePushCheckVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    finding: str = ""


class ModelLanePushAttempt(BaseModel):
    """What the pusher saw: its exit code and the remote head it read back."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exit_code: int
    remote_head_after: str


class ModelLaneRulingState(BaseModel):
    """The lane's claim and its rulings as read from the bus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_found: bool
    unacknowledged_rulings: tuple[str, ...] = ()


class ModelLanePushResult(BaseModel):
    """The one answer: pushed, refused with the reason, or moved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: EnumLanePushOutcome
    correlation_id: UUID
    repository: str
    branch: str
    remote_head: str | None = None
    refusal: EnumLanePushRefusal | None = None
    refused_check: str | None = None
    reason: str | None = None


__all__ = [
    "NULL_HEAD",
    "EnumLanePushOutcome",
    "EnumLanePushRefusal",
    "ModelLanePushAttempt",
    "ModelLanePushCheckVerdict",
    "ModelLanePushRequest",
    "ModelLanePushResult",
    "ModelLanePushSource",
    "ModelLaneRulingState",
]
