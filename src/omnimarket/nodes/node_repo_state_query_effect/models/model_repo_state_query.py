# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O of node_repo_state_query_effect (OMN-23033).

A query names one host, one repository and what it wants: the refs, a branch head, files
at a revision, or a pull request's state, checks or one job's record. The answer always
comes from the host's mirror (the canonical clone for git state, the PR watcher's state
for pull request, check and job state). It says whether the mirror held the latest state
the bus knows of when it answered (``current``), whether this query waited on a pull
(``pulled``) and what that pull did, and how many comparisons the query cost.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_repo_merged import validate_git_sha, validate_repo_slug

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = r"^[A-Za-z0-9_./-]+$"


def is_full_sha(value: str | None) -> bool:
    return bool(value) and bool(_SHA_RE.match(value or ""))


class EnumRepoStateWant(StrEnum):
    """What a query asks for."""

    REFS = "refs"
    BRANCH_HEAD = "branch_head"
    FILES = "files"
    PULL_REQUEST = "pull_request"
    CHECKS = "checks"
    JOB_LOG = "job_log"


class EnumRepoStateMirror(StrEnum):
    """Which of the host's mirrors answers a want, and which one a pull refreshes."""

    CLONE = "clone"
    WATCHER = "watcher"


MIRROR_FOR_WANT: dict[EnumRepoStateWant, EnumRepoStateMirror] = {
    EnumRepoStateWant.REFS: EnumRepoStateMirror.CLONE,
    EnumRepoStateWant.BRANCH_HEAD: EnumRepoStateMirror.CLONE,
    EnumRepoStateWant.FILES: EnumRepoStateMirror.CLONE,
    EnumRepoStateWant.PULL_REQUEST: EnumRepoStateMirror.WATCHER,
    EnumRepoStateWant.CHECKS: EnumRepoStateMirror.WATCHER,
    EnumRepoStateWant.JOB_LOG: EnumRepoStateMirror.WATCHER,
}


class EnumRepoStateSource(StrEnum):
    """Where an answer came from.

    ``mirror``: the mirror already held the latest state the bus knows of; the query cost
    the comparison only. ``mirror_after_pull``: the mirror was behind, one pull (shared by
    every concurrent query of the same repository and mirror) brought it current, and the
    answer was read after it. ``mirror_behind``: the pull ran and the mirror is still
    behind; the answer is the mirror's and says so. ``unavailable``: the mirror could not
    answer.
    """

    MIRROR = "mirror"
    MIRROR_AFTER_PULL = "mirror_after_pull"
    MIRROR_BEHIND = "mirror_behind"
    UNAVAILABLE = "unavailable"


class ModelRepoStateQuery(BaseModel):
    """One state query: host, repository and want."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    host: str = Field(
        min_length=1, max_length=64, description="The host whose mirror answers."
    )
    repo: str = Field(description="owner/name on GitHub")
    want: EnumRepoStateWant
    branch: str | None = Field(default=None, max_length=255, pattern=_REF_RE)
    revision: str | None = Field(
        default=None,
        max_length=255,
        pattern=_REF_RE,
        description="A full sha or a branch name.",
    )
    paths: tuple[str, ...] = Field(default=(), max_length=64)
    pr_number: int | None = Field(default=None, ge=1)
    job_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _check(self) -> ModelRepoStateQuery:
        validate_repo_slug(self.repo)
        if self.want is EnumRepoStateWant.FILES and not self.paths:
            raise ValueError("want files needs at least one path")
        if any(p.startswith("/") or ".." in p.split("/") for p in self.paths):
            raise ValueError("paths are repository-relative and never climb out")
        if (
            MIRROR_FOR_WANT[self.want] is EnumRepoStateMirror.WATCHER
            and self.pr_number is None
        ):
            raise ValueError(f"want {self.want.value} needs pr_number")
        if self.want is EnumRepoStateWant.JOB_LOG and self.job_id is None:
            raise ValueError("want job_log needs job_id")
        return self


class ModelBusRepoHead(BaseModel):
    """The latest head of one branch the bus has carried (a merge or push event)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    branch: str = Field(min_length=1)
    sha: str
    observed_at: Annotated[datetime, AwareDatetime]
    source: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _check(self) -> ModelBusRepoHead:
        validate_repo_slug(self.repo)
        validate_git_sha(self.sha)
        return self


class ModelBusPrEvent(BaseModel):
    """The latest event the bus has carried for one pull request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    pr_number: int = Field(ge=1)
    state: str = Field(min_length=1, max_length=16)
    sha: str
    observed_at: Annotated[datetime, AwareDatetime]


class ModelMirrorPull(BaseModel):
    """What one pull of one mirror did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    mirror: EnumRepoStateMirror
    status: str = Field(min_length=1, max_length=32)
    ok: bool
    target_sha: str = ""
    before_sha: str = ""
    after_sha: str = ""
    waiters: int = Field(default=1, ge=1, description="Queries that shared this pull.")
    duration_ms: int = Field(default=0, ge=0)
    message: str = Field(default="", max_length=2000)


class ModelRepoStateFile(BaseModel):
    """One file read from the mirror at a revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    present: bool
    content: str | None = None
    truncated: bool = False


class ModelRepoStateAnswer(BaseModel):
    """The answer to one query, the node's terminal event payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str
    repo: str
    want: EnumRepoStateWant
    ok: bool
    source: EnumRepoStateSource
    current: bool = Field(
        description="The mirror held the bus's latest state when it answered."
    )
    comparisons: int = Field(ge=0)
    pulled: bool = False
    pull: ModelMirrorPull | None = None
    mirror_sha: str | None = None
    bus_sha: str | None = None
    bus_observed_at: datetime | None = None
    revision_sha: str | None = None
    refs: dict[str, str] | None = None
    files: tuple[ModelRepoStateFile, ...] | None = None
    record: dict[str, Any] | None = Field(
        default=None,
        description="The PR watcher's record (pull_request), its ci (checks) or one job.",
    )
    mirror_read_at: datetime | None = None
    error: str | None = Field(default=None, max_length=2000)


class ModelRepoStateTopics(BaseModel):
    """The node's topics, read from its contract and nowhere else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: str
    success: str
    failure: str
    bus_state: tuple[str, ...] = Field(min_length=1)


__all__ = [
    "MIRROR_FOR_WANT",
    "EnumRepoStateMirror",
    "EnumRepoStateSource",
    "EnumRepoStateWant",
    "ModelBusPrEvent",
    "ModelBusRepoHead",
    "ModelMirrorPull",
    "ModelRepoStateAnswer",
    "ModelRepoStateFile",
    "ModelRepoStateQuery",
    "ModelRepoStateTopics",
    "is_full_sha",
]
