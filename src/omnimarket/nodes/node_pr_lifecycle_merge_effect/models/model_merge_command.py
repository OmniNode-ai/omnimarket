# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrMergeCommand — command to execute auto-merge for a green PR.

Related:
    - OMN-8084: Create pr_lifecycle_merge_effect Node
    - OMN-15483: carry the observed hold-marker surfaces (title + labels) so the
      merge path can refuse a PR that is explicitly held
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelPrMergeCommand(BaseModel):
    """Command to execute auto-merge for a PR classified green by triage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Merge run correlation ID.")
    pr_number: int = Field(..., description="PR number to merge.", gt=0)
    repo: str = Field(..., description="GitHub repo slug (owner/repo).")
    triage_verdict: str = Field(
        ..., description="Triage verdict that triggered this merge (must be 'green')."
    )
    use_merge_queue: bool = Field(
        default=False,
        description=(
            "True for merge-queue repos (--auto, no method); False for --squash --auto."
        ),
    )
    ticket_id: str | None = Field(
        default=None, description="Linear ticket ID for context."
    )
    pr_title: str | None = Field(
        default=None,
        description=(
            "PR title as observed at inventory time — the first surface the "
            "OMN-15483 hold marker is matched against. None (or blank) means "
            "the title was NOT observed, which is unknown, never 'clear': a "
            "command carrying neither a title nor labels is refused."
        ),
    )
    pr_labels: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "PR label names as observed at inventory time — the second hold "
            "surface. None means the label set was NOT observed; the empty "
            "tuple means it WAS observed and the PR carries no labels "
            "(OMN-14151 tri-state idiom; None never decays to 'no labels')."
        ),
    )
    dry_run: bool = Field(default=False, description="Run without side effects.")
    requested_at: datetime = Field(..., description="When the command was issued.")


__all__: list[str] = ["ModelPrMergeCommand"]
