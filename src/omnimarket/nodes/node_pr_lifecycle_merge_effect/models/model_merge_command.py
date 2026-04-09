# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrMergeCommand — command to execute auto-merge for a green PR.

Related:
    - OMN-8084: Create pr_lifecycle_merge_effect Node
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
    dry_run: bool = Field(default=False, description="Run without side effects.")
    requested_at: datetime = Field(..., description="When the command was issued.")


__all__: list[str] = ["ModelPrMergeCommand"]
