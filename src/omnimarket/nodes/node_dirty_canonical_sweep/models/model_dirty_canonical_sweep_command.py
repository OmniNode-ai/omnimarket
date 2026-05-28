# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input command for node_dirty_canonical_sweep."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDirtyCanonicalSweepCommand(BaseModel):
    """Command envelope for a dirty-canonical-sweep run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    omni_home: str | None = Field(
        default=None,
        description="Override OMNI_HOME path. Resolved from $OMNI_HOME if not set.",
    )
    worktrees_root: str | None = Field(
        default=None,
        description=(
            "Override worktrees root path. Resolved from $OMNI_WORKTREES_ROOT "
            "or $OMNI_HOME/omni_worktrees if not set."
        ),
    )
    repos: list[str] | None = Field(
        default=None,
        description=(
            "Explicit list of repo directory names to check. "
            "If None, all immediate subdirectories of omni_home containing a "
            ".git directory are checked."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="Detect dirty repos and report without moving files or creating PRs.",
    )
    pr_label: str = Field(
        default="auto-ship",
        description="GitHub label to attach to auto-shipped PRs.",
    )
    base_branch: str = Field(
        default="dev",
        description="Git branch to create rescue worktrees from and target PRs against.",
    )


__all__: list[str] = ["ModelDirtyCanonicalSweepCommand"]
