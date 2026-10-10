# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O for node_github_repo_gateway_effect.

A single request model selects one read *operation*; each operation returns its
own typed result shape (a Pydantic-discriminated union keyed on ``operation``).
Callers never receive a bare ``dict`` — every operation resolves to a concrete,
small typed object suitable for a verify-before-accept loop.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumGithubGatewayOperation(StrEnum):
    """The read operations this gateway supports.

    The last six (OMN-20912) cover the reads lanes made with ``gh run list``,
    ``gh run view``, job logs, ``gh run download``, ``gh pr view`` for text and
    comments, and ``gh release list``.
    """

    PR_STATUS = "pr_status"
    CI_CHECKS = "ci_checks"
    OPEN_PRS_LIST = "open_prs_list"
    BRANCH_PROTECTION = "branch_protection"
    REVIEW_GATE = "review_gate"
    MERGE_COMMIT_SHA = "merge_commit_sha"
    TICKET_REF = "ticket_ref"
    WORKFLOW_RUNS = "workflow_runs"
    RUN_JOBS = "run_jobs"
    JOB_LOG_TAIL = "job_log_tail"
    ARTIFACTS = "artifacts"
    PR_TEXT = "pr_text"
    RELEASES = "releases"


# Operations scoped to a single PR require ``pr_number``.
_PR_SCOPED_OPERATIONS: frozenset[EnumGithubGatewayOperation] = frozenset(
    {
        EnumGithubGatewayOperation.PR_STATUS,
        EnumGithubGatewayOperation.CI_CHECKS,
        EnumGithubGatewayOperation.REVIEW_GATE,
        EnumGithubGatewayOperation.MERGE_COMMIT_SHA,
        EnumGithubGatewayOperation.TICKET_REF,
        EnumGithubGatewayOperation.PR_TEXT,
    }
)
# Operations scoped to one workflow run require ``run_id``.
_RUN_SCOPED_OPERATIONS: frozenset[EnumGithubGatewayOperation] = frozenset(
    {
        EnumGithubGatewayOperation.RUN_JOBS,
        EnumGithubGatewayOperation.ARTIFACTS,
    }
)

# Bounds. A caller names a smaller bound; it can never name a larger one. Every
# result travels as one bus event, so the byte caps stay well under a broker's
# default 1 MiB message size even after base64 (4/3).
DEFAULT_LIST_LIMIT = 30
MAX_LIST_LIMIT = 300
DEFAULT_LOG_TAIL_BYTES = 16 * 1024
MAX_LOG_TAIL_BYTES = 64 * 1024
DEFAULT_ARTIFACT_MAX_BYTES = 256 * 1024
MAX_ARTIFACT_MAX_BYTES = 512 * 1024
# A PR body or comment longer than this is cut, and flagged as cut.
MAX_TEXT_CHARS = 16 * 1024


class ModelGithubGatewayRequest(BaseModel):
    """Input contract: pick one read operation against a repo (and maybe a PR)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumGithubGatewayOperation = Field(
        ..., description="Which read operation to run."
    )
    repo: str = Field(
        ...,
        description="GitHub repo slug (org/name).",
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    pr_number: int | None = Field(
        default=None,
        description="PR number; required for PR-scoped operations, ignored otherwise.",
        gt=0,
    )
    run_id: int | None = Field(
        default=None,
        gt=0,
        description="Workflow run id; required by run_jobs and artifacts.",
    )
    job_id: int | None = Field(
        default=None, gt=0, description="Job id; required by job_log_tail."
    )
    artifact_id: int | None = Field(
        default=None,
        gt=0,
        description="artifacts only: also fetch this artifact's archive, bounded.",
    )
    branch: str | None = Field(
        default=None,
        min_length=1,
        description="workflow_runs only: runs on this branch.",
    )
    head_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
        description="workflow_runs only: runs at this head commit.",
    )
    workflow: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="workflow_runs only: a workflow file name (ci.yml) or id.",
    )
    event: str | None = Field(
        default=None,
        pattern=r"^[a-z_]+$",
        description="workflow_runs only: the triggering event (push, pull_request, ...).",
    )
    status: str | None = Field(
        default=None,
        pattern=r"^[a-z_]+$",
        description="workflow_runs only: a run status or conclusion (completed, failure, ...).",
    )
    limit: int = Field(
        default=DEFAULT_LIST_LIMIT,
        ge=1,
        le=MAX_LIST_LIMIT,
        description="Most items a list read returns; the node pages internally.",
    )
    tail_bytes: int = Field(
        default=DEFAULT_LOG_TAIL_BYTES,
        ge=1,
        le=MAX_LOG_TAIL_BYTES,
        description="job_log_tail only: bytes kept from the end of the log.",
    )
    max_bytes: int = Field(
        default=DEFAULT_ARTIFACT_MAX_BYTES,
        ge=1,
        le=MAX_ARTIFACT_MAX_BYTES,
        description="artifacts only: largest archive fetched; larger is refused.",
    )
    correlation_id: UUID = Field(
        default_factory=uuid4,
        description="Correlation ID flowing through the pipeline (auto if omitted).",
    )

    @model_validator(mode="after")
    def _require_scope_fields(self) -> ModelGithubGatewayRequest:
        if self.operation in _PR_SCOPED_OPERATIONS and self.pr_number is None:
            raise ValueError(
                f"operation {self.operation.value!r} requires pr_number to be set."
            )
        if self.operation in _RUN_SCOPED_OPERATIONS and self.run_id is None:
            raise ValueError(
                f"operation {self.operation.value!r} requires run_id to be set."
            )
        if (
            self.operation is EnumGithubGatewayOperation.JOB_LOG_TAIL
            and self.job_id is None
        ):
            raise ValueError("operation 'job_log_tail' requires job_id to be set.")
        if (
            self.artifact_id is not None
            and self.operation is not EnumGithubGatewayOperation.ARTIFACTS
        ):
            raise ValueError("artifact_id is read only by the artifacts operation.")
        return self


# --- discriminated result models (one shape per operation) ------------------

_OverallState = Literal["green", "red", "pending"]


class ModelCheckContext(BaseModel):
    """One required status/check context on a PR head commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., description="Context or check-run name.")
    state: _OverallState = Field(..., description="Normalized pass/fail/pending state.")


class ModelPrStatusResult(BaseModel):
    """Merge-readiness summary for a single PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["pr_status"] = "pr_status"
    repo: str
    pr_number: int
    overall: _OverallState = Field(
        ..., description="Rolled-up state of required checks."
    )
    blocked: bool = Field(..., description="True when the PR cannot merge right now.")
    merge_state_status: str = Field(
        ..., description="GitHub mergeStateStatus (CLEAN/BLOCKED/DIRTY/...)."
    )
    review_decision: str | None = Field(
        default=None,
        description="APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED / None.",
    )
    failing_contexts: list[str] = Field(
        default_factory=list, description="Names of required checks not passing."
    )


class ModelCiChecksResult(BaseModel):
    """Required-check rollup with per-state counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["ci_checks"] = "ci_checks"
    repo: str
    pr_number: int
    overall: _OverallState
    total: int = Field(..., ge=0, description="Number of required checks considered.")
    passed: int = Field(..., ge=0)
    failed: int = Field(..., ge=0)
    pending: int = Field(..., ge=0)
    failing_contexts: list[str] = Field(default_factory=list)


class ModelOpenPrSummary(BaseModel):
    """Compact summary of one open PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    title: str
    is_draft: bool
    merge_state_status: str
    review_decision: str | None = None


class ModelOpenPrsResult(BaseModel):
    """List of open PRs for a repo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["open_prs_list"] = "open_prs_list"
    repo: str
    count: int = Field(..., ge=0)
    prs: list[ModelOpenPrSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def _count_matches_prs(self) -> ModelOpenPrsResult:
        if self.count != len(self.prs):
            raise ValueError("count must match len(prs).")
        return self


class ModelBranchProtectionResult(BaseModel):
    """Branch-protection review requirement for a repo's default branch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["branch_protection"] = "branch_protection"
    repo: str
    required_approving_review_count: int | None = Field(
        default=None,
        description="Required approving reviews, or None if unprotected.",
    )


class ModelReviewGateResult(BaseModel):
    """Review-gate state for a single PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["review_gate"] = "review_gate"
    repo: str
    pr_number: int
    review_decision: str | None = None
    unresolved_threads: int = Field(
        ..., description="Count of unresolved review threads."
    )
    blocked: bool = Field(
        ..., description="CHANGES_REQUESTED or any unresolved thread present."
    )


class ModelMergeCommitShaResult(BaseModel):
    """Merge outcome for a single PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["merge_commit_sha"] = "merge_commit_sha"
    repo: str
    pr_number: int
    merged: bool
    merge_commit_sha: str | None = None


class ModelTicketRefResult(BaseModel):
    """Linear ticket reference extracted from a PR head branch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["ticket_ref"] = "ticket_ref"
    repo: str
    pr_number: int
    head_ref: str
    ticket_id: str | None = Field(
        default=None, description="OMN-#### token found in the head branch, if any."
    )


# --- OMN-20912: Actions, PR text and release reads ---------------------------


class _ModelBoundedList(BaseModel):
    """Shared fields of a list read: GitHub's total and whether it was cut."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_count: int | None = Field(
        default=None,
        ge=0,
        description="GitHub's own total for the query, when the endpoint reports one.",
    )
    truncated: bool = Field(
        ..., description="True when more items existed than the request's limit."
    )


class ModelWorkflowRunSummary(BaseModel):
    """One workflow run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    name: str
    workflow_id: int
    head_branch: str | None = None
    head_sha: str
    event: str
    status: str | None = None
    conclusion: str | None = None
    run_number: int
    run_attempt: int
    created_at: str
    updated_at: str
    html_url: str


class ModelWorkflowRunsResult(_ModelBoundedList):
    """Workflow runs of a repo, newest first (replaces ``gh run list``)."""

    operation: Literal["workflow_runs"] = "workflow_runs"
    repo: str
    runs: list[ModelWorkflowRunSummary] = Field(default_factory=list)


class ModelJobStep(BaseModel):
    """One step of a job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    name: str
    status: str
    conclusion: str | None = None


class ModelRunJob(BaseModel):
    """One job of a workflow run, any attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    name: str
    status: str
    conclusion: str | None = None
    run_attempt: int
    started_at: str | None = None
    completed_at: str | None = None
    html_url: str | None = None
    steps: list[ModelJobStep] = Field(default_factory=list)


class ModelRunJobsResult(_ModelBoundedList):
    """Every job of one run across its attempts (replaces ``gh run view``)."""

    operation: Literal["run_jobs"] = "run_jobs"
    repo: str
    run_id: int
    jobs: list[ModelRunJob] = Field(default_factory=list)


class ModelJobLogTailResult(BaseModel):
    """The end of one job's log, at most the request's tail_bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["job_log_tail"] = "job_log_tail"
    repo: str
    job_id: int
    total_bytes: int = Field(..., ge=0, description="Length of the whole log.")
    truncated: bool = Field(..., description="True when the log's head was dropped.")
    text: str = Field(..., description="The kept tail, decoded as UTF-8.")


class EnumArtifactFetchRefusal(StrEnum):
    """Why an artifact archive was not fetched."""

    OVER_SIZE_CAP = "over_size_cap"
    EXPIRED = "expired"
    NOT_IN_RUN = "not_in_run"


class ModelArtifactSummary(BaseModel):
    """One artifact of a workflow run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    name: str
    size_in_bytes: int = Field(..., ge=0)
    expired: bool
    digest: str | None = None
    created_at: str | None = None
    expires_at: str | None = None


class ModelArtifactContent(BaseModel):
    """One artifact's archive (a zip), fetched under the size cap."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: int
    name: str
    size_bytes: int = Field(..., ge=0)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    content_base64: str


class ModelArtifactsResult(_ModelBoundedList):
    """Artifacts of one run, and one bounded archive when asked (``gh run download``)."""

    operation: Literal["artifacts"] = "artifacts"
    repo: str
    run_id: int
    artifacts: list[ModelArtifactSummary] = Field(default_factory=list)
    fetched: ModelArtifactContent | None = None
    fetch_refused: EnumArtifactFetchRefusal | None = Field(
        default=None,
        description="Set when an artifact_id was asked for and not fetched.",
    )

    @model_validator(mode="after")
    def _fetched_or_refused(self) -> ModelArtifactsResult:
        if self.fetched is not None and self.fetch_refused is not None:
            raise ValueError("an artifact is either fetched or refused, not both.")
        return self


class ModelPrComment(BaseModel):
    """One conversation comment on a PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    author: str
    created_at: str
    updated_at: str
    body: str
    body_truncated: bool


class ModelPrTextResult(_ModelBoundedList):
    """A PR's title, body and conversation comments (``gh pr view --comments``).

    ``total_count`` and ``truncated`` describe the comments.
    """

    operation: Literal["pr_text"] = "pr_text"
    repo: str
    pr_number: int
    title: str
    body: str
    body_truncated: bool
    state: str
    is_draft: bool
    merged: bool
    author: str
    head_ref: str
    head_sha: str
    base_ref: str
    comments: list[ModelPrComment] = Field(default_factory=list)


class ModelRelease(BaseModel):
    """One release."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    tag_name: str
    name: str | None = None
    draft: bool
    prerelease: bool
    target_commitish: str
    created_at: str
    published_at: str | None = None
    html_url: str


class ModelTag(BaseModel):
    """One tag and the commit it points at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    sha: str


class ModelReleasesResult(BaseModel):
    """Releases and tags of a repo, newest first (``gh release list``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["releases"] = "releases"
    repo: str
    releases: list[ModelRelease] = Field(default_factory=list)
    releases_truncated: bool
    tags: list[ModelTag] = Field(default_factory=list)
    tags_truncated: bool


GithubGatewayResult = Annotated[
    ModelPrStatusResult
    | ModelCiChecksResult
    | ModelOpenPrsResult
    | ModelBranchProtectionResult
    | ModelReviewGateResult
    | ModelMergeCommitShaResult
    | ModelTicketRefResult
    | ModelWorkflowRunsResult
    | ModelRunJobsResult
    | ModelJobLogTailResult
    | ModelArtifactsResult
    | ModelPrTextResult
    | ModelReleasesResult,
    Field(discriminator="operation"),
]


class ModelGithubGatewayResponse(BaseModel):
    """Runtime event wrapper carrying the discriminated result.

    The CLI prints the inner ``result`` directly (the small typed object); the
    runtime effect handler emits this wrapper as its terminal event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    result: GithubGatewayResult


__all__: list[str] = [
    "DEFAULT_ARTIFACT_MAX_BYTES",
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_LOG_TAIL_BYTES",
    "MAX_ARTIFACT_MAX_BYTES",
    "MAX_LIST_LIMIT",
    "MAX_LOG_TAIL_BYTES",
    "MAX_TEXT_CHARS",
    "EnumArtifactFetchRefusal",
    "EnumGithubGatewayOperation",
    "GithubGatewayResult",
    "ModelArtifactContent",
    "ModelArtifactSummary",
    "ModelArtifactsResult",
    "ModelBranchProtectionResult",
    "ModelCheckContext",
    "ModelCiChecksResult",
    "ModelGithubGatewayRequest",
    "ModelGithubGatewayResponse",
    "ModelJobLogTailResult",
    "ModelJobStep",
    "ModelMergeCommitShaResult",
    "ModelOpenPrSummary",
    "ModelOpenPrsResult",
    "ModelPrComment",
    "ModelPrStatusResult",
    "ModelPrTextResult",
    "ModelRelease",
    "ModelReleasesResult",
    "ModelReviewGateResult",
    "ModelRunJob",
    "ModelRunJobsResult",
    "ModelTag",
    "ModelTicketRefResult",
    "ModelWorkflowRunSummary",
    "ModelWorkflowRunsResult",
]
