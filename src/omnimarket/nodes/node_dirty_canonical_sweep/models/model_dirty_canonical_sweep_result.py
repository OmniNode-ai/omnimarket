# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result models for node_dirty_canonical_sweep."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelDirtyRepoShipResult(BaseModel):
    """Outcome for one dirty canonical repo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Repository directory name.")
    dirty_files: list[str] = Field(..., description="Relative paths of dirty files.")
    status: Literal["shipped", "dry_run", "failed"] = Field(
        ..., description="Outcome for this repo."
    )
    worktree_path: str = Field(
        default="",
        description="Absolute path to the created worktree (empty on dry_run or failure).",
    )
    branch: str = Field(
        default="",
        description="Branch name created in the worktree (empty on dry_run or failure).",
    )
    pr_url: str = Field(
        default="",
        description="URL of the created PR (empty on dry_run or failure).",
    )
    error: str = Field(
        default="",
        description="Error detail when status == failed.",
    )


class ModelDirtyCanonicalSweepResult(BaseModel):
    """Terminal result for a dirty-canonical-sweep run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos_checked: int = Field(..., description="Total repos inspected.")
    repos_dirty: int = Field(..., description="Repos that had uncommitted changes.")
    repos_shipped: int = Field(..., description="Repos successfully shipped to PRs.")
    repos_failed: int = Field(..., description="Repos where ship failed.")
    dry_run: bool = Field(..., description="Whether this was a dry-run invocation.")
    results: list[ModelDirtyRepoShipResult] = Field(
        default_factory=list,
        description="Per-repo outcomes (only dirty repos are included).",
    )


__all__: list[str] = [
    "ModelDirtyCanonicalSweepResult",
    "ModelDirtyRepoShipResult",
]
