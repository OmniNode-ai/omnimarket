# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a host lets the worktree reconcile node remove (the policy a host overlay sets)."""

from pydantic import BaseModel, ConfigDict, Field


class ModelWorktreeReconcilePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    quiet_hours: float = Field(default=6.0, ge=0, allow_inf_nan=False)
    stale_hours: float = Field(default=72.0, ge=0, allow_inf_nan=False)
    allow_standalone_clone_removal: bool = True
    max_removals_per_run: int = Field(default=200, ge=0)
    # A standalone clone whose origin owner is not listed is always kept (a
    # driver source tree or a third-party tool is not a lane's scratch clone).
    # Empty means no standalone clone qualifies.
    allowed_remote_owners: tuple[str, ...] = ()
    # True: a tree is removed only when its work is merged (content_merged or
    # merged_pr_head). Every other removal rule, and every model verdict that would
    # remove, is held at KEEP with the rule's reason, so a host can be limited to
    # the merged class by its overlay.
    merged_only: bool = False
