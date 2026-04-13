# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelAutoMergeResult -- output contract for node_auto_merge_effect."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelAutoMergeResult(BaseModel):
    """Output from the auto-merge effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Correlation ID from the input.")
    pr_number: int = Field(..., description="PR number that was acted upon.")
    repo: str = Field(..., description="GitHub repo slug.")
    merged: bool = Field(..., description="Whether the PR was successfully merged.")
    merge_commit_sha: str | None = Field(
        default=None, description="Merge commit SHA, or None if not merged."
    )
    blocked_reason: str | None = Field(
        default=None, description="Human-readable reason the merge was blocked."
    )
    ticket_close_status: str | None = Field(
        default=None,
        description="Linear ticket close outcome: 'closed' | 'skipped' | 'failed' | None.",
    )


__all__: list[str] = ["ModelAutoMergeResult"]
