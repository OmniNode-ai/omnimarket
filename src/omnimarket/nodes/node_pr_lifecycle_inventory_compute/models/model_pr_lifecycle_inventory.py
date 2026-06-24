# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for pr_lifecycle_inventory_compute node.

Related:
    - OMN-8082: Create pr_lifecycle_inventory_compute Node
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelPrCheckRun(BaseModel):
    """A single CI check run result."""

    name: str
    status: str  # queued | in_progress | completed
    conclusion: str | None = None  # success | failure | cancelled | skipped | neutral
    event: str | None = (
        None  # GitHub run trigger: pull_request | workflow_dispatch | merge_group | ...
    )


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
    reviews: tuple[ModelPrReview, ...] = Field(default_factory=tuple)
    has_conflicts: bool = False
    ci_passing: bool | None = None  # None when checks not yet complete


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
