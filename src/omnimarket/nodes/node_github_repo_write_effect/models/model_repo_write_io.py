# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O of node_github_repo_write_effect (OMN-20912).

One request names one write operation; the effect answers with one completed
or one failed result. Each operation replaces one ``gh`` verb a lane ran:

==================  ===========================  =============================
operation           gh verb replaced             GitHub call
==================  ===========================  =============================
pr_create           gh pr create                 POST /pulls
pr_edit             gh pr edit                   PATCH /pulls/{n}; GraphQL draft
pr_ready            gh pr ready                  GraphQL markPullRequestReadyForReview
pr_close            gh pr close                  PATCH /pulls/{n} state=closed
pr_comment          gh pr comment                POST /issues/{n}/comments
workflow_dispatch   gh workflow run              POST /actions/workflows/{w}/dispatches
==================  ===========================  =============================
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_landing_github.model_github_quota_reading import (
    ModelGithubQuotaReading,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
)

# GitHub's own limits: a PR or comment body holds at most 65536 characters and
# a workflow_dispatch carries at most 10 inputs.
MAX_BODY_CHARS = 65_536
MAX_DISPATCH_INPUTS = 10

_SHA = r"^[0-9a-f]{40}$"
_REF = r"^[A-Za-z0-9._/-]+$"


class EnumRepoWriteOperation(StrEnum):
    """The write operations, named for the gh verb each replaces."""

    PR_CREATE = "pr_create"
    PR_EDIT = "pr_edit"
    PR_READY = "pr_ready"
    PR_CLOSE = "pr_close"
    PR_COMMENT = "pr_comment"
    WORKFLOW_DISPATCH = "workflow_dispatch"


class EnumRepoWriteMode(StrEnum):
    """dry_run plans the calls and sends nothing; enforce sends them."""

    DRY_RUN = "dry_run"
    ENFORCE = "enforce"


class EnumRepoWriteRefusal(StrEnum):
    """Why a write was refused or failed. No mutation follows a refusal."""

    QUOTA_FLOOR = "quota_floor"
    HEAD_MOVED = "head_moved"
    CLAIM_NOT_HELD = "claim_not_held"
    NOT_FOUND = "not_found"
    GITHUB_REFUSED = "github_refused"
    TRANSPORT_ERROR = "transport_error"


_PR_SCOPED = frozenset(
    {
        EnumRepoWriteOperation.PR_EDIT,
        EnumRepoWriteOperation.PR_READY,
        EnumRepoWriteOperation.PR_CLOSE,
        EnumRepoWriteOperation.PR_COMMENT,
    }
)
# Operations that change a PR's state name the head they expect to act on.
_HEAD_REQUIRED = frozenset(
    {
        EnumRepoWriteOperation.PR_EDIT,
        EnumRepoWriteOperation.PR_READY,
        EnumRepoWriteOperation.PR_CLOSE,
    }
)


class ModelRepoWriteRequest(BaseModel):
    """One write operation against one repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    operation: EnumRepoWriteOperation
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Stable per intended write; a retried command with the same "
        "key does not write twice.",
    )
    requested_by_lane: str = Field(
        min_length=1, max_length=128, description="The lane asking for the write."
    )
    requested_by_run: str | None = Field(
        default=None,
        min_length=1,
        description="pr_close: the run id the claim was taken under.",
    )
    pr_number: int | None = Field(default=None, gt=0)
    expected_head_sha: str | None = Field(
        default=None,
        pattern=_SHA,
        description="The head the caller expects; a moved head is refused with "
        "head_moved before any mutation. Required by pr_edit, pr_ready and "
        "pr_close; checked against the branch tip for pr_create and "
        "workflow_dispatch when given.",
    )
    title: str | None = Field(default=None, min_length=1, max_length=256)
    body: str | None = Field(default=None, max_length=MAX_BODY_CHARS)
    head: str | None = Field(default=None, pattern=_REF, description="pr_create.")
    base: str | None = Field(default=None, pattern=_REF)
    draft: bool | None = None
    workflow: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="workflow_dispatch: a workflow file name (ci.yml) or id.",
    )
    ref: str | None = Field(
        default=None, pattern=_REF, description="workflow_dispatch: the ref to run."
    )
    inputs: dict[str, str] = Field(default_factory=dict)
    claims_dir: str | None = Field(
        default=None,
        min_length=1,
        description="pr_close: the directory node_pr_claim_registry_effect keeps "
        "claims in, as the registry itself takes it.",
    )
    dry_run: bool = Field(
        default=False,
        description="Force dry_run even where the contract's mode is enforce.",
    )

    @model_validator(mode="after")
    def _operation_fields(self) -> ModelRepoWriteRequest:
        op = self.operation
        missing: list[str] = []
        if op in _PR_SCOPED and self.pr_number is None:
            missing.append("pr_number")
        if op in _HEAD_REQUIRED and self.expected_head_sha is None:
            missing.append("expected_head_sha")
        if op is EnumRepoWriteOperation.PR_CREATE:
            missing += [
                n
                for n, v in (
                    ("head", self.head),
                    ("base", self.base),
                    ("title", self.title),
                )
                if v is None
            ]
        if op is EnumRepoWriteOperation.PR_COMMENT and not self.body:
            missing.append("body")
        if op is EnumRepoWriteOperation.PR_CLOSE:
            missing += [
                n
                for n, v in (
                    ("claims_dir", self.claims_dir),
                    ("requested_by_run", self.requested_by_run),
                )
                if v is None
            ]
        if op is EnumRepoWriteOperation.WORKFLOW_DISPATCH:
            missing += [
                n
                for n, v in (("workflow", self.workflow), ("ref", self.ref))
                if v is None
            ]
        if missing:
            raise ValueError(f"operation {op.value!r} requires {', '.join(missing)}")
        if op is EnumRepoWriteOperation.PR_EDIT and all(
            v is None for v in (self.title, self.body, self.base, self.draft)
        ):
            raise ValueError("pr_edit needs at least one of title, body, base, draft")
        if len(self.inputs) > MAX_DISPATCH_INPUTS:
            raise ValueError(
                f"workflow_dispatch takes at most {MAX_DISPATCH_INPUTS} inputs"
            )
        if self.inputs and op is not EnumRepoWriteOperation.WORKFLOW_DISPATCH:
            raise ValueError("inputs is read only by workflow_dispatch")
        return self

    @property
    def pr_key(self) -> str:
        """The claim registry's canonical key, lowercase org/repo#number."""
        return f"{self.repo.lower()}#{self.pr_number}"


class _ModelRepoWriteResultBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    operation: EnumRepoWriteOperation
    repo: str
    mode: EnumRepoWriteMode
    idempotency_key: str
    requested_by_lane: str
    pr_number: int | None = None
    requests: tuple[ModelGithubHttpRequest, ...] = Field(
        default=(),
        description="The requests sent (enforce) or planned (dry_run), in order.",
    )
    http_statuses: tuple[int, ...] = ()
    quota: ModelGithubQuotaReading | None = None


class ModelRepoWriteCompleted(_ModelRepoWriteResultBase):
    """The write ran (or, in dry_run, the calls it would make)."""

    outcome: Literal["completed"] = "completed"
    replayed: bool = Field(
        default=False,
        description="True when the write had already happened: the same "
        "idempotency key, an open PR for the head, a comment carrying the key, "
        "or a PR already in the asked-for state. Nothing was written again.",
    )
    html_url: str | None = None
    head_sha: str | None = None
    comment_id: int | None = None


class ModelRepoWriteFailed(_ModelRepoWriteResultBase):
    """The write was refused or failed; no mutation followed a refusal.

    ``terminal_failure_cause`` is the field the runtime's failure-terminal
    guard reads (omnibase_infra ``resolve_terminal_verdict``): a returned model
    carrying a cause is published on the contract's declared failure terminal.
    It always equals ``reason``.
    """

    outcome: Literal["failed"] = "failed"
    reason: EnumRepoWriteRefusal
    terminal_failure_cause: EnumRepoWriteRefusal | None = None
    detail: str = Field(min_length=1)
    http_status: int | None = None
    retry_after_seconds: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _cause_is_the_reason(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("terminal_failure_cause") is None:
            return {**data, "terminal_failure_cause": data.get("reason")}
        return data

    @model_validator(mode="after")
    def _cause_matches(self) -> ModelRepoWriteFailed:
        if self.terminal_failure_cause is not self.reason:
            raise ValueError("terminal_failure_cause must equal reason")
        return self


__all__: list[str] = [
    "MAX_BODY_CHARS",
    "MAX_DISPATCH_INPUTS",
    "EnumRepoWriteMode",
    "EnumRepoWriteOperation",
    "EnumRepoWriteRefusal",
    "ModelRepoWriteCompleted",
    "ModelRepoWriteFailed",
    "ModelRepoWriteRequest",
]
