# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O of node_github_repo_write_effect (OMN-20912).

One request names one write operation; the effect answers with one completed
or one failed result. Each operation replaces one ``gh`` verb a lane ran:

- pr_create replaces ``gh pr create`` with POST /pulls.
- pr_edit replaces ``gh pr edit`` with PATCH /pulls/{n} and GraphQL draft.
- pr_ready replaces ``gh pr ready`` with GraphQL markPullRequestReadyForReview.
- pr_close replaces ``gh pr close`` with PATCH /pulls/{n} state=closed.
- pr_comment replaces ``gh pr comment`` with POST /issues/{n}/comments.
- workflow_dispatch replaces ``gh workflow run`` with
  POST /actions/workflows/{w}/dispatches.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    CONTRACT_REQUIRED = "contract_required"
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


def parse_claim_stamp(stamp: str) -> datetime:
    """A claim timestamp as the claim registry writes it, naive meaning UTC."""
    parsed = datetime.fromisoformat(stamp.rstrip("Z"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class ModelRepoWriteClaim(BaseModel):
    """The PR claim a caller took, carried on a pr_close command (OMN-21067).

    The claim file lives in the caller's claim registry, on the caller's host;
    the effect runs elsewhere and cannot read it. The caller reads its own
    claim and sends these fields, the ones the registry records when a claim
    is taken. The effect checks them against the request and the clock; the
    claim registry it can read still decides when it holds a claim on the PR.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_key: str = Field(min_length=1, description="Canonical lowercase org/repo#n.")
    lane_id: str = Field(min_length=1, description="The lane that took the claim.")
    claimed_by_run: str = Field(min_length=1, description="The run it was taken under.")
    action: str = Field(min_length=1, description="What the holder is doing.")
    claimed_at: str = Field(min_length=1, description="UTC time the claim was taken.")
    last_heartbeat_at: str | None = Field(
        default=None, description="UTC time of the last heartbeat, when it has one."
    )

    @model_validator(mode="after")
    def _timestamps_parse(self) -> ModelRepoWriteClaim:
        for name in ("claimed_at", "last_heartbeat_at"):
            stamp = getattr(self, name)
            if stamp is None:
                continue
            try:
                parse_claim_stamp(stamp)
            except ValueError as exc:
                raise ValueError(f"{name} {stamp!r} is not a UTC timestamp") from exc
        return self

    @property
    def latest_stamp(self) -> datetime:
        """The later of the claim time and the last heartbeat."""
        stamps = [parse_claim_stamp(self.claimed_at)]
        if self.last_heartbeat_at is not None:
            stamps.append(parse_claim_stamp(self.last_heartbeat_at))
        return max(stamps)


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
    claim: ModelRepoWriteClaim | None = Field(
        default=None,
        description="pr_close: the claim the caller took, read from the caller's "
        "own registry and carried here because the effect cannot read that "
        "directory. Without one, the effect's own registry must hold the claim.",
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
        description="pr_close: a directory node_pr_claim_registry_effect keeps "
        "claims in, as the effect's runtime sees it. Not the caller's directory.",
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
        if op is EnumRepoWriteOperation.PR_CLOSE and self.requested_by_run is None:
            missing.append("requested_by_run")
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
    "ModelRepoWriteClaim",
    "ModelRepoWriteCompleted",
    "ModelRepoWriteFailed",
    "ModelRepoWriteRequest",
    "parse_claim_stamp",
]
