# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelConflictResolvedEvent — output contract for node_conflict_hunk_effect."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelConflictResolvedEvent(BaseModel):
    """Output emitted after a conflict resolution attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Correlation ID from the input.")
    pr_number: int = Field(..., description="PR number acted upon.")
    repo: str = Field(..., description="GitHub repo slug (org/repo).")
    head_ref_name: str = Field(..., description="Branch whose conflicts were resolved.")
    resolved_files: list[str] = Field(
        default_factory=list,
        description="Files that had conflict markers resolved.",
    )
    resolution_committed: bool = Field(
        ..., description="Whether a commit was created in the worktree."
    )
    is_noop: bool = Field(
        default=False,
        description="True when LLM output matched existing file; no commit made.",
    )
    commit_sha: str | None = Field(
        default=None,
        description="Git commit SHA of the resolution commit, or None if no commit.",
    )
    used_fallback: bool = Field(
        default=False,
        description="True if the fallback LLM model was used.",
    )
    error: str | None = Field(
        default=None,
        description="Error message if resolution was not successful.",
    )
    success: bool = Field(
        ..., description="True when all conflict files were resolved without error."
    )


__all__: list[str] = ["ModelConflictResolvedEvent"]
