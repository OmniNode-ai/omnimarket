# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What holds a worktree: its lock, its processes and claims, and a gitdir that is gone (OMN-24560)."""

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision as Decision,
)
from omnimarket.events.worktree_reconcile import ModelWorktreeFacts
from omnimarket.models.model_worktree_reconcile_policy import (
    ModelWorktreeReconcilePolicy,
)


def stale_lock(facts: ModelWorktreeFacts, policy: ModelWorktreeReconcilePolicy) -> bool:
    """Whether the tree's worktree lock marker is stale: nothing backs it.

    A lane's CLAIM, a process working in the tree or an installed unit naming it
    holds the lock. A ref lock (index.lock or HEAD.lock) means a git command is
    running or died mid-write, so the lock marker is never called stale while
    one exists. The ledger is the authority: live_claim is False unless a live
    CLAIM names this tree (a lane with a TERMINAL or no live CLAIM names none),
    and that absence counts only when the run read a ledger (ledger_known); a
    host that declares none keeps every locked tree.
    """
    return bool(
        facts.worktree_locked
        and facts.ledger_known
        and not facts.ref_locked
        and not facts.live_process
        and not facts.live_claim
        and not facts.live_unit
        and facts.last_activity_age_hours is not None
        and facts.last_activity_age_hours >= policy.stale_lock_hours
    )


def live_reason(
    facts: ModelWorktreeFacts, policy: ModelWorktreeReconcilePolicy
) -> str | None:
    for reason in ("live_process", "live_claim", "live_unit"):
        if getattr(facts, reason):
            return reason
    if facts.git_locked and not stale_lock(facts, policy):
        return "git_locked"
    if facts.in_progress_operation:
        return "in_progress_op"
    if (
        facts.last_activity_age_hours is None
        or facts.last_activity_age_hours < policy.quiet_hours
    ):
        return "recent_activity"
    return None


def dead_decision(
    facts: ModelWorktreeFacts, policy: ModelWorktreeReconcilePolicy, age: float
) -> tuple[Decision, str]:
    """A dead tree holds no checkout anyone can reach; git cannot even read its head.

    A tree this young may be a lane mid-setup, so it waits for the stale age like any other
    work; past it, the overlay's prune knob decides between the trash and a person.
    """
    if age < policy.stale_hours:
        return Decision.KEEP, "not_stale"
    return (
        (Decision.REMOVE, "dead_worktree")
        if policy.prune_dead_worktrees
        else (Decision.NEEDS_HUMAN, "dead_worktree")
    )
