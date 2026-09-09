# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for pr_lifecycle_inventory_compute node.

Related:
    - OMN-8082: Create pr_lifecycle_inventory_compute Node
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.merge_control.reason_code_classifier import EnumMergeCheckReasonCode


class ModelPrCheckRun(BaseModel):
    """A single CI check run result."""

    name: str
    status: str  # queued | in_progress | completed
    conclusion: str | None = None  # success | failure | cancelled | skipped | neutral
    event: str | None = (
        None  # GitHub run trigger: pull_request | workflow_dispatch | merge_group | ...
    )
    link: str = Field(default="", description="GitHub details URL for this check.")
    flaky_failure_evidence: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Machine evidence that the failed check is an infrastructure/network "
            "flake rather than a product-code failure."
        ),
    )
    reason_code: EnumMergeCheckReasonCode | None = Field(
        default=None,
        description=(
            "Typed merge-check reason code (OMN-14765) derived from the "
            "jobs-API attempt (runs/<id>/jobs) for a FAILED check: one of "
            "stale_context | github_api_outage | runner_infra | cancelled | "
            "product_failed. None means never classified (green check, or the "
            "jobs-API call was unavailable — treated as unknown, never product)."
        ),
    )


class ModelPrCheckExecution(BaseModel):
    """One immutable GitHub check-run execution for a PR head.

    This is separate from :class:`ModelPrCheckRun`: the latter is the current
    ``gh pr checks`` view used by merge decisions, while this model preserves
    individual executions returned by ``check-runs?filter=all``. A check
    execution is a GitHub check-run, not a count of validators inside a bundled
    CI job.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_run_id: int = Field(..., ge=1)
    name: str = Field(..., min_length=1)
    status: str = Field(..., min_length=1)
    conclusion: str | None = None
    head_sha: str = Field(..., pattern=r"^[0-9a-f]{40}$")
    check_suite_id: int | None = Field(default=None, ge=1)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    details_url: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _timestamps_must_be_timezone_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("check-execution timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def _validate_duration(self) -> ModelPrCheckExecution:
        if self.started_at is None or self.completed_at is None:
            if self.duration_seconds is not None:
                raise ValueError("duration requires both check-execution timestamps")
            return self

        expected_duration = (self.completed_at - self.started_at).total_seconds()
        if expected_duration < 0:
            raise ValueError("check-execution completion precedes start")
        if self.duration_seconds is None:
            raise ValueError("duration is required when both timestamps are present")
        if abs(self.duration_seconds - expected_duration) > 0.000001:
            raise ValueError("duration does not match check-execution timestamps")
        return self


class ModelPrReview(BaseModel):
    """A single PR review record."""

    author: str
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED


class ModelPrInventoryInput(BaseModel):
    """Input for pr_lifecycle_inventory_compute.

    Specifies which PRs to collect state for.
    """

    repo: str = Field(..., description="GitHub repo slug, e.g. OmniNode-ai/omnimarket")
    pr_numbers: tuple[int, ...] = Field(
        ..., description="PR numbers to collect state for"
    )
    include_check_execution_history: bool = Field(
        default=False,
        description=(
            "When true, collect immutable GitHub check-run executions for each "
            "PR head using check-runs?filter=all. The default preserves the "
            "current-state-only inventory behavior."
        ),
    )


class ModelPrState(BaseModel):
    """Raw collected state for a single PR.

    Pure data — no classification or action logic.
    """

    repo: str
    pr_number: int
    title: str
    state: Literal["open", "closed", "merged"]
    is_draft: bool = False
    mergeable: str | None = None  # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state_status: str | None = None  # CLEAN | DIRTY | BLOCKED | UNKNOWN
    review_decision: str | None = None  # APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED
    head_ref: str = ""
    base_ref: str = ""
    check_runs: tuple[ModelPrCheckRun, ...] = Field(default_factory=tuple)
    check_execution_history_requested: bool = Field(
        default=False,
        description=(
            "Whether the caller requested immutable check-execution history. "
            "Together with check_execution_history_error, this distinguishes "
            "an opt-out from a successful zero-row collection."
        ),
    )
    check_executions: tuple[ModelPrCheckExecution, ...] = Field(default_factory=tuple)
    check_execution_history_error: str | None = Field(
        default=None,
        description=(
            "Collection failure for the opt-in immutable check-execution history. "
            "A non-null value means an empty history is unavailable, never a "
            "claim that the PR had zero check executions."
        ),
    )
    reviews: tuple[ModelPrReview, ...] = Field(default_factory=tuple)
    has_conflicts: bool = False
    ci_passing: bool | None = None  # None when checks not yet complete
    coderabbit_unresolved: int | None = Field(
        default=None,
        description=(
            "Count of unresolved CodeRabbit review threads (OMN-14151). None "
            "means the count was never collected — callers must treat that as "
            "unknown, never as 0."
        ),
    )


class ModelStuckQueueEntry(BaseModel):
    """A PR that has been in the merge queue longer than the stuck threshold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    title: str
    queue_entered_at: datetime
    queue_age_minutes: float
    queue_state: str = "QUEUED"
    head_sha: str | None = None
    merge_group_run_count: int | None = None


class ModelOrgWideOpenPrRemainder(BaseModel):
    """A single org-wide open PR that blocks a sweep-done report (OMN-13318)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Repo slug, e.g. OmniNode-ai/omnibase_infra.")
    pr_number: int = Field(..., description="Open PR number.")
    title: str = Field(default="", description="PR title for human-readable triage.")
    url: str = Field(default="", description="HTML URL of the open PR.")


class ModelOrgWideOpenPrInventory(BaseModel):
    """Org-wide open-PR census used as the sweep-done precondition (OMN-13318).

    The overnight sweep can falsely look complete when a repo-by-repo memory
    misses an open PR (e.g. omnibase_infra#2043 still open after two merged).
    This census is the single org-wide source of truth: ``open_count`` is the
    hard precondition on the done-report, and ``remainders`` lists exactly which
    PRs still block "sweep done".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    open_count: int = Field(
        ...,
        ge=0,
        description=(
            "Org-wide count of open PRs from "
            "'gh api /search/issues?q=org:OmniNode-ai is:pr is:open'."
        ),
    )
    remainders: tuple[ModelOrgWideOpenPrRemainder, ...] = Field(
        default_factory=tuple,
        description="The open PRs that still block a sweep-done report.",
    )
    query_failed: bool = Field(
        default=False,
        description=(
            "True if the org-wide search could not be executed. A failed query "
            "must be treated as NOT_DONE — never silently reported as done."
        ),
    )

    @property
    def sweep_done(self) -> bool:
        """The sweep may only report done when zero open PRs remain org-wide.

        A failed query is fail-closed: it is never ``done``.
        """
        return self.open_count == 0 and not self.query_failed


class ModelPrInventoryOutput(BaseModel):
    """Output of pr_lifecycle_inventory_compute.

    Contains raw PR state for each requested PR.
    """

    repo: str
    pr_states: tuple[ModelPrState, ...] = Field(default_factory=tuple)
    total_collected: int = 0
    collection_errors: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Errors encountered during collection (e.g. PR not found)",
    )
    stuck_queue_prs: list[ModelStuckQueueEntry] = Field(
        default_factory=list,
        description=(
            "PRs queued past a stuck threshold or AWAITING_CHECKS without "
            "merge_group runs past the dispatch-stall threshold."
        ),
    )
    org_wide_open: ModelOrgWideOpenPrInventory | None = Field(
        default=None,
        description=(
            "Org-wide open-PR census (OMN-13318). Populated on every full "
            "inventory call; the orchestrator refuses to report sweep-done "
            "while open_count > 0."
        ),
    )
