# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrInventoryItem — a single PR's collected inventory state.

This model represents the raw data produced by pr_lifecycle_inventory_compute.
The triage node consumes these items and classifies each one.

Related:
    - OMN-8082: pr_lifecycle_inventory_compute (producer)
    - OMN-8083: pr_lifecycle_triage_compute (consumer)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPrInventoryItem(BaseModel):
    """Raw inventory state for a single PR, produced by the inventory node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(..., description="GitHub PR number.")
    repo: str = Field(
        ..., description="Repository slug (e.g. 'OmniNode-ai/omnimarket')."
    )
    title: str = Field(default="", description="PR title.")
    branch: str = Field(default="", description="Head branch name.")
    ci_status: str = Field(
        default="unknown",
        description=(
            "Overall CI status from gh pr checks. "
            "Expected values: 'passing', 'failing', 'pending', 'skipped', 'unknown'."
        ),
    )
    has_conflicts: bool = Field(
        default=False,
        description="True if the PR has merge conflicts.",
    )
    approved: bool = Field(
        default=False,
        description="True if the PR has at least one approving review and no pending CHANGES_REQUESTED.",
    )
    review_count: int = Field(
        default=0,
        ge=0,
        description="Number of completed reviews (approved + changes_requested).",
    )
    open_threads: int = Field(
        default=0,
        ge=0,
        description="Number of unresolved review threads.",
    )


__all__: list[str] = ["ModelPrInventoryItem"]
