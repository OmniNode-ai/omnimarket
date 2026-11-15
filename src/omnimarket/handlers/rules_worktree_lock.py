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

    Only a lane's own lock may be judged: the reason in the marker must be one of
    policy.lane_lock_reasons (what the lane runner writes), so a person's lock, a
    tool's lock or a lock with no reason is never stale. Then the ledger is the
    authority, and only a fresh read of it counts (ledger_known): it must map the
    tree to a lane CLAIM that a TERMINAL closed with no open CLAIM naming it
    (owner_finished), and no live CLAIM of a successor may map to it (live_claim). A process working in the tree, an installed unit naming it,
    or a ref lock (index.lock or HEAD.lock: a git command is running or died
    mid-write) holds the lock whatever the ledger says.
    """
    return bool(
        facts.worktree_locked
        and facts.worktree_lock_reason.strip() in policy.lane_lock_reasons
        and facts.ledger_known
        and facts.owner_finished
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
    work; past it, the tree is named for a person: a dead gitdir is never removed, only
    reported.
    """
    if age < policy.stale_hours:
        return Decision.KEEP, "not_stale"
    return Decision.NEEDS_HUMAN, "gitdir_unreadable"
