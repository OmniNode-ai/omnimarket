# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrMergeResult — result from the pr_lifecycle_merge_effect node.

Related:
    - OMN-8084: Create pr_lifecycle_merge_effect Node
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
    completed_at: datetime = Field(..., description="When the merge completed.")


__all__: list[str] = ["ModelPrMergeResult"]
