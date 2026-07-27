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
    """The read operations this gateway supports (first slice: reads only)."""

    PR_STATUS = "pr_status"
    CI_CHECKS = "ci_checks"
    OPEN_PRS_LIST = "open_prs_list"
    BRANCH_PROTECTION = "branch_protection"
    REVIEW_GATE = "review_gate"
    MERGE_COMMIT_SHA = "merge_commit_sha"
    TICKET_REF = "ticket_ref"


# Operations scoped to a single PR require ``pr_number``; the two repo-scoped
# operations do not.
_PR_SCOPED_OPERATIONS: frozenset[EnumGithubGatewayOperation] = frozenset(
    {
        EnumGithubGatewayOperation.PR_STATUS,
        EnumGithubGatewayOperation.CI_CHECKS,
        EnumGithubGatewayOperation.REVIEW_GATE,
        EnumGithubGatewayOperation.MERGE_COMMIT_SHA,
        EnumGithubGatewayOperation.TICKET_REF,
    }
)


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
    correlation_id: UUID = Field(
        default_factory=uuid4,
        description="Correlation ID flowing through the pipeline (auto if omitted).",
    )

    @model_validator(mode="after")
    def _require_pr_number_for_pr_scoped(self) -> ModelGithubGatewayRequest:
        if self.operation in _PR_SCOPED_OPERATIONS and self.pr_number is None:
            raise ValueError(
                f"operation {self.operation.value!r} requires pr_number to be set."
            )
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


GithubGatewayResult = Annotated[
    ModelPrStatusResult
    | ModelCiChecksResult
    | ModelOpenPrsResult
    | ModelBranchProtectionResult
    | ModelReviewGateResult
    | ModelMergeCommitShaResult
    | ModelTicketRefResult,
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
    "EnumGithubGatewayOperation",
    "GithubGatewayResult",
    "ModelBranchProtectionResult",
    "ModelCheckContext",
    "ModelCiChecksResult",
    "ModelGithubGatewayRequest",
    "ModelGithubGatewayResponse",
    "ModelMergeCommitShaResult",
    "ModelOpenPrSummary",
    "ModelOpenPrsResult",
    "ModelPrStatusResult",
    "ModelReviewGateResult",
    "ModelTicketRefResult",
]
