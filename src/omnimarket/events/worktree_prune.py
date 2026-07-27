# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared event models for the worktree-prune effect (OMN-13859).

These live in the shared ``omnimarket.events`` package — not inside the
node — so both ``node_pr_lifecycle_worktree_prune_effect`` (the producer of the
result / consumer of the command) and ``node_pr_lifecycle_orchestrator`` (which
builds the command in POST_MERGE_TAIL) import them without a cross-node
reach-in. See ``tests/test_no_cross_node_reach_in.py`` and the omnimarket
CLAUDE.md rule: "Promote shared types instead."

Related:
    - OMN-13859: Event-driven worktree prune-on-PR-close.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelWorktreePruneCommand(BaseModel):
    """Command to prune a single ticket/repo git worktree after PR close.

    The worktree path is resolved deterministically as
    ``<worktrees_root>/<ticket_id>/<repo_name>`` where ``repo_name`` is the bare
    repository name (owner prefix stripped). ``worktrees_root`` is resolved from
    this field when set, else from the ``ONEX_WORKTREES_ROOT`` env var — never a
    hardcoded path (OMN-13859 rail #4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Sweep run correlation ID.")
    ticket_id: str = Field(
        ...,
        min_length=1,
        description="Canonical OMN ticket identifier owning the worktree (e.g. OMN-13859).",
    )
    repo: str = Field(
        ...,
        min_length=1,
        description="Repo slug or bare name; the owner prefix is stripped for path resolution.",
    )
    branch: str | None = Field(
        default=None,
        description=(
            "Branch that just closed, for provenance/logging only. NOT required — "
            "the trigger event (PR merged/closed) is the source of truth, so a "
            "deleted remote branch or missing @{u} upstream never blocks prune "
            "(OMN-13859 rail #3)."
        ),
    )
    pr_number: int | None = Field(
        default=None,
        gt=0,
        description="PR number that closed, for provenance/logging only.",
    )
    worktrees_root: str | None = Field(
        default=None,
        description=(
            "Explicit worktrees root override. When None, resolved from the "
            "ONEX_WORKTREES_ROOT env var (fail-loud if unset)."
        ),
    )
    merge_target_ref: str | None = Field(
        default=None,
        description=(
            "Git ref the PR merged into (e.g. 'origin/dev'), used to prove that a "
            "dirty worktree's content is already landed (OMN-15251). When None, no "
            "reachability proof is possible and any dirty worktree is preserved as "
            "SKIPPED_DIRTY — the classifier fails closed."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="Classify the worktree and report the intended action without removing it.",
    )


class EnumPruneOutcome(StrEnum):
    """Terminal classification for a worktree prune attempt.

    Only ``PRUNED`` removes anything. Every ``SKIPPED_*`` / ``REFUSED_*`` outcome
    leaves the worktree untouched — the safety rails always fail toward *keep*,
    never toward *remove*.
    """

    PRUNED = "pruned"
    # OMN-15251 — dirty, but every dirty path's working content was positively
    # proven reachable from the merge target, so nothing recoverable is lost.
    # Removed, and distinguished from PRUNED so the audit trail shows why.
    PRUNED_SUPERSEDED = "pruned_superseded"
    DRY_RUN = "dry_run"
    # Rail #1 — uncommitted changes present that are NOT provably landed;
    # flagged for recovery, NOT removed. Also the fail-closed landing point for
    # every inconclusive reachability check (OMN-15251).
    SKIPPED_DIRTY = "skipped_dirty"
    # No filesystem path (worktree already gone / never created) — idempotent no-op.
    SKIPPED_NOT_FOUND = "skipped_not_found"
    # Path exists but is not a registered git worktree (no worktree .git file).
    SKIPPED_NOT_A_WORKTREE = "skipped_not_a_worktree"
    # Rail #2 — resolved path escapes the worktrees root, or is a canonical clone
    # (its .git is a directory, not a worktree gitlink file). Refused.
    REFUSED_OUTSIDE_ROOT = "refused_outside_root"
    # Removal command errored.
    FAILED = "failed"


class ModelWorktreePruneResult(BaseModel):
    """Result from the pr_lifecycle worktree prune effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Sweep run correlation ID.")
    ticket_id: str = Field(..., description="Ticket whose worktree was targeted.")
    repo: str = Field(..., description="Repo (bare name) whose worktree was targeted.")
    worktree_path: str | None = Field(
        default=None,
        description="Resolved absolute worktree path considered (None if unresolvable).",
    )
    outcome: EnumPruneOutcome = Field(..., description="Terminal prune classification.")
    dirty_file_count: int = Field(
        default=0,
        ge=0,
        description="Count of dirty (uncommitted) paths when outcome is SKIPPED_DIRTY.",
    )
    unreachable_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "Repo-relative dirty paths whose content could NOT be proven reachable "
            "from the merge target (OMN-15251). Non-empty only for SKIPPED_DIRTY, "
            "and it is precisely this list the operator decision request is about. "
            "Empty on SKIPPED_DIRTY means the check was inconclusive (no merge "
            "target, or a probe error) rather than positively divergent."
        ),
    )
    detail: str = Field(
        default="", description="Human-readable explanation of the outcome."
    )
    error: str | None = Field(
        default=None,
        description="Error message when outcome is FAILED (null otherwise).",
    )
    completed_at: datetime = Field(..., description="When the prune attempt completed.")


__all__: list[str] = [
    "EnumPruneOutcome",
    "ModelWorktreePruneCommand",
    "ModelWorktreePruneResult",
]
