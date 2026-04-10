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
        description="PRs that have been queued past the stuck threshold (default 30 min).",
    )
