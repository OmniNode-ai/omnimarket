# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Removal limits for the worktree reconcile rules: the cap and the merged-only hold."""

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision as Decision,
)
from omnimarket.events.worktree_reconcile import (
    ModelWorktreeReconcileDecisions,
)
from omnimarket.events.worktree_reconcile_policy import ModelWorktreeReconcilePolicy

MERGED_REASONS = frozenset({"content_merged", "pr_merged_head"})
REMOVING = (Decision.REMOVE, Decision.PIN_AND_REMOVE)


def hold_unmerged(
    decision: Decision, reason: str, policy: ModelWorktreeReconcilePolicy
) -> tuple[Decision, str]:
    """A merged-only host removes only merged work; every other removal is held at keep."""
    if policy.merged_only and decision in REMOVING and reason not in MERGED_REASONS:
        return Decision.KEEP, f"merged_only:{reason}"
    return decision, reason


def refuses_model_removal(
    verdict: Decision, policy: ModelWorktreeReconcilePolicy
) -> bool:
    """A merged-only host takes no model advice to remove: the model cannot produce merge evidence."""
    return policy.merged_only and verdict in REMOVING


def cap_removals(
    decisions: ModelWorktreeReconcileDecisions,
) -> ModelWorktreeReconcileDecisions:
    used = 0
    rows = []
    for row in decisions.decisions:
        if row.decision in REMOVING:
            used += 1
            if used > decisions.policy.max_removals_per_run:
                row = row.model_copy(
                    update={"decision": Decision.KEEP, "reasons": ("removal_cap",)}
                )
        rows.append(row)
    return decisions.model_copy(update={"decisions": tuple(rows)})
