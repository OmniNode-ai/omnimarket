# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrMergeResult — result from the pr_lifecycle_merge_effect node.

Related:
    - OMN-8084: Create pr_lifecycle_merge_effect Node
    - OMN-15483: surface the hold-eligibility decision so a sweep receipt shows
      WHY a PR was skipped, not merely that it was
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelPrMergeResult(BaseModel):
    """Result from the pr_lifecycle merge effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Merge run correlation ID.")
    pr_number: int = Field(..., description="PR number that was merged.")
    repo: str = Field(..., description="GitHub repo slug (owner/repo).")
    merged: bool = Field(
        ..., description="Whether the merge was executed (or would be in dry_run)."
    )
    merge_action: str = Field(
        ..., description="Human-readable description of the merge action taken."
    )
    error: str | None = Field(
        default=None, description="Error message if merge failed (null on success)."
    )
    hold_status: str | None = Field(
        default=None,
        description=(
            "OMN-15483 hold verdict for this PR: 'clear' (merge permitted), "
            "'held' (a hold marker matched), or 'indeterminate' (hold state "
            "unreadable — refused, treated as held). None only on results that "
            "predate the hold evaluation (e.g. a non-green verdict rejected "
            "before the probe runs)."
        ),
    )
    hold_matched_token: str | None = Field(
        default=None,
        description=(
            "The exact marker substring that held this PR, verbatim from the "
            "matched surface. Null when not held."
        ),
    )
    hold_matched_source: str | None = Field(
        default=None,
        description=(
            "Which surface carried the marker: 'title' or 'label'. Null when not held."
        ),
    )
    hold_unobserved_sources: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Hold surfaces the probe could NOT see. Non-empty on a 'clear' "
            "verdict means the clear is only partial — an unobserved surface "
            "could still be carrying a marker."
        ),
    )
    completed_at: datetime = Field(..., description="When the merge completed.")


__all__: list[str] = ["ModelPrMergeResult"]
