# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O for node_git_query_mirror_effect (OMN-19617).

One request picks one operation against one repository (and, for the
PR-scoped operations, one pull request). Every answer says where it came from
(``source``) and how old the mirror was when it answered (``watermark_age_s``),
so a caller can tell a local-git answer from a GitHub API fallback.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_MAX_AGE_S = 300
MAX_DIFF_CHARS = 2_000_000


class EnumGitQueryOperation(StrEnum):
    """The operations the clone query layer answers."""

    SYNC = "sync"
    HEAD_SHA = "head_sha"
    CHANGED_FILES = "changed_files"
    DIFF = "diff"
    CONFLICTS = "conflicts"
    ON_BASE = "on_base"
    LOG = "log"
    STATUS = "status"


PR_SCOPED_OPERATIONS: frozenset[EnumGitQueryOperation] = frozenset(
    {
        EnumGitQueryOperation.HEAD_SHA,
        EnumGitQueryOperation.CHANGED_FILES,
        EnumGitQueryOperation.DIFF,
        EnumGitQueryOperation.CONFLICTS,
        EnumGitQueryOperation.ON_BASE,
        EnumGitQueryOperation.LOG,
    }
)


class EnumGitQuerySource(StrEnum):
    """Where an answer came from.

    ``mirror``: the mirror was fresh and held the refs. ``mirror_refreshed``:
    the mirror was stale or lacked the ref, and one targeted git fetch (git
    transport, no GitHub API quota) brought it current first.
    ``gh_fallback``: the targeted fetch failed, and the answer came from the
    GitHub API. ``unavailable``: no source could answer.
    """

    MIRROR = "mirror"
    MIRROR_REFRESHED = "mirror_refreshed"
    GH_FALLBACK = "gh_fallback"
    UNAVAILABLE = "unavailable"


class EnumOnBaseReason(StrEnum):
    """Why ``on_base`` holds its value."""

    HEAD_IS_ANCESTOR = "head_is_ancestor"
    MERGE_ADDS_NOTHING = "merge_adds_nothing"
    MERGE_ADDS_CHANGES = "merge_adds_changes"
    CONFLICTS = "conflicts"


class ModelGitQueryRequest(BaseModel):
    """Pick one operation against one repository (and maybe one PR)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumGitQueryOperation
    repo: str = Field(
        ...,
        description="GitHub repository slug, owner/name.",
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    pr_number: int | None = Field(default=None, ge=1)
    base: str | None = Field(
        default=None,
        description="Base branch name; defaults to the mirror's default branch.",
        pattern=r"^[A-Za-z0-9_./-]+$",
    )
    max_age_s: int = Field(
        default=DEFAULT_MAX_AGE_S,
        ge=0,
        description="Refresh the PR ref by git fetch first when the mirror is older.",
    )
    allow_gh_fallback: bool = Field(
        default=True,
        description="Fall back to the GitHub API when the targeted git fetch fails.",
    )
    correlation_id: UUID = Field(default_factory=uuid4)

    @model_validator(mode="after")
    def _pr_scoped_needs_pr(self) -> ModelGitQueryRequest:
        if self.operation in PR_SCOPED_OPERATIONS and self.pr_number is None:
            raise ValueError(f"operation {self.operation.value} needs pr_number")
        return self


class ModelGitCommit(BaseModel):
    """One commit on the PR branch, oldest first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str
    subject: str
    author: str
    authored_at: str


class ModelGitQueryResult(BaseModel):
    """The answer to one operation. Fields an operation does not fill stay None."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumGitQueryOperation
    repo: str
    pr_number: int | None = None
    ok: bool
    source: EnumGitQuerySource
    watermark_age_s: float | None = Field(
        default=None, description="Seconds since the mirror's last full fetch."
    )
    mirror_path: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    merge_base: str | None = None
    files: list[str] | None = None
    diff: str | None = Field(default=None, max_length=MAX_DIFF_CHARS)
    diff_truncated: bool = False
    conflicts: bool | None = None
    conflicted_files: list[str] | None = None
    on_base: bool | None = None
    on_base_reason: EnumOnBaseReason | None = None
    commits: list[ModelGitCommit] | None = None
    pull_refs: int | None = None
    head_refs: int | None = None
    error: str | None = None


class ModelGitQueryResponse(BaseModel):
    """Correlation-tagged wrapper, the node's terminal event payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    results: tuple[ModelGitQueryResult, ...]


__all__ = [
    "DEFAULT_MAX_AGE_S",
    "MAX_DIFF_CHARS",
    "PR_SCOPED_OPERATIONS",
    "EnumGitQueryOperation",
    "EnumGitQuerySource",
    "EnumOnBaseReason",
    "ModelGitCommit",
    "ModelGitQueryRequest",
    "ModelGitQueryResponse",
    "ModelGitQueryResult",
]
