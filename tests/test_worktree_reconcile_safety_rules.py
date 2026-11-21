# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Safety rails: a locked tree is never removed, a dead tree is never pruned.

Every case is paired with a CONTROL that differs in exactly one fact and gives
the opposite decision.
"""

import pytest
from pydantic import ValidationError

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeKind as Kind,
)
from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision as Decision,
)
from omnimarket.events.worktree_reconcile import ModelWorktreeFacts
from omnimarket.handlers.rules_worktree_removal import hold_unmerged
from omnimarket.models.model_worktree_reconcile_policy import (
    ModelWorktreeReconcilePolicy,
)
from omnimarket.worktree_reconcile.rules import classify

pytestmark = pytest.mark.unit


def facts(**updates: object) -> ModelWorktreeFacts:
    if updates.get("worktree_locked"):
        # The probe reports a lock marker as git_locked too.
        updates.setdefault("git_locked", True)
    return ModelWorktreeFacts(
        host="host",
        path="trees/task/repo",
        root="trees",
        kind=Kind.LINKED_WORKTREE,
        head_sha="abc",
        remote_owner="owner",
        last_activity_age_hours=100,
    ).model_copy(update=updates)


def test_person_lock_is_kept_and_lane_reason_is_removed() -> None:
    policy = ModelWorktreeReconcilePolicy(
        allowed_remote_owners=("owner",),
        lane_lock_reasons=("lane-a",),
    )
    # A person lock: the reason is not a lane reason, so the lock holds.
    person = facts(
        worktree_locked=True,
        worktree_lock_reason="held by a person",
        ledger_known=True,
        owner_finished=True,
        content_merged=True,
        last_activity_age_hours=100,
    )
    assert classify(person, policy) == (Decision.KEEP, "git_locked")
    # CONTROL: the same facts with the reason equal to an entry of
    # policy.lane_lock_reasons: the lane is gone, so the tree is removed.
    lane = person.model_copy(update={"worktree_lock_reason": "lane-a"})
    assert classify(lane, policy) == (Decision.REMOVE, "content_merged")


def test_empty_lane_lock_reasons_default_keeps_locked_tree() -> None:
    policy = ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",))
    locked = facts(
        worktree_locked=True,
        worktree_lock_reason="lane-a",
        ledger_known=True,
        owner_finished=True,
        content_merged=True,
    )
    # With no lane reasons declared, a locked tree is never stale.
    assert classify(locked, policy) == (Decision.KEEP, "git_locked")
    # CONTROL: a tree with no lock marker is removed.
    unlocked = locked.model_copy(update={"worktree_locked": False, "git_locked": False})
    assert classify(unlocked, policy) == (Decision.REMOVE, "content_merged")


def test_lagging_ledger_keeps_locked_tree() -> None:
    policy = ModelWorktreeReconcilePolicy(
        allowed_remote_owners=("owner",),
        lane_lock_reasons=("lane-a",),
    )
    # The ledger was not read: a lane reason proves nothing, so the lock holds.
    lagging = facts(
        worktree_locked=True,
        worktree_lock_reason="lane-a",
        ledger_known=False,
        owner_finished=True,
        content_merged=True,
        last_activity_age_hours=100,
    )
    assert classify(lagging, policy) == (Decision.KEEP, "git_locked")
    # CONTROL: the ledger is known, so the lane reason counts and the tree is
    # removed.
    known = lagging.model_copy(update={"ledger_known": True})
    assert classify(known, policy) == (Decision.REMOVE, "content_merged")


def test_owner_not_finished_keeps_locked_tree() -> None:
    policy = ModelWorktreeReconcilePolicy(
        allowed_remote_owners=("owner",),
        lane_lock_reasons=("lane-a",),
    )
    # The ledger says the owner has not finished: the lock holds.
    not_finished = facts(
        worktree_locked=True,
        worktree_lock_reason="lane-a",
        ledger_known=True,
        owner_finished=False,
        content_merged=True,
        last_activity_age_hours=100,
    )
    assert classify(not_finished, policy) == (Decision.KEEP, "git_locked")
    # CONTROL: the owner finished, so the lock is stale and the tree is removed.
    finished = not_finished.model_copy(update={"owner_finished": True})
    assert classify(finished, policy) == (Decision.REMOVE, "content_merged")


def test_uncommitted_work_keeps_locked_tree() -> None:
    policy = ModelWorktreeReconcilePolicy(
        allowed_remote_owners=("owner",),
        lane_lock_reasons=("lane-a",),
    )
    # A locked-stale tree with dirty tracked files: the dirty rule decides.
    dirty = facts(
        worktree_locked=True,
        worktree_lock_reason="lane-a",
        ledger_known=True,
        owner_finished=True,
        dirty_tracked_count=1,
        content_merged=True,
        last_activity_age_hours=100,
    )
    assert classify(dirty, policy) == (Decision.NEEDS_HUMAN, "dirty")
    # Same with untracked non-junk files.
    untracked = dirty.model_copy(
        update={"dirty_tracked_count": 0, "untracked_nonjunk_count": 1}
    )
    assert classify(untracked, policy) == (Decision.NEEDS_HUMAN, "dirty")
    # CONTROL: no uncommitted work, so the tree is removed.
    clean = dirty.model_copy(update={"dirty_tracked_count": 0})
    assert classify(clean, policy) == (Decision.REMOVE, "content_merged")


def test_unpushed_branch_keeps_locked_tree() -> None:
    policy = ModelWorktreeReconcilePolicy(
        allowed_remote_owners=("owner",), lane_lock_reasons=("lane-a",)
    )
    # A locked-stale tree with unpushed commits past the stale age: never
    # pinned and removed.
    locked = facts(
        worktree_locked=True,
        worktree_lock_reason="lane-a",
        ledger_known=True,
        owner_finished=True,
        commits_not_on_remote=2,
        last_activity_age_hours=100,
    )
    assert classify(locked, policy) == (Decision.KEEP, "locked_unpushed_work")
    # CONTROL: the same tree unlocked is pinned and removed on a host without
    # merged_only.
    unlocked = locked.model_copy(update={"worktree_locked": False, "git_locked": False})
    assert classify(unlocked, policy) == (
        Decision.PIN_AND_REMOVE,
        "unpushed_work_stale",
    )


def test_dead_tree_is_never_removed() -> None:
    policy = ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",))
    # A dead tree past the stale age is named for a person, never removed.
    dead = facts(gitdir_missing=True, last_activity_age_hours=100)
    assert classify(dead, policy) == (Decision.NEEDS_HUMAN, "gitdir_unreadable")
    # Even with merged_only False, a dead tree is never removed.
    assert classify(dead, policy) != (Decision.REMOVE, "dead_worktree")
    # A young dead tree is kept as not stale.
    young = facts(gitdir_missing=True, last_activity_age_hours=10)
    assert classify(young, policy) == (Decision.KEEP, "not_stale")


def test_merged_only_host_holds_locked_stale_finished_tree() -> None:
    policy = ModelWorktreeReconcilePolicy(
        allowed_remote_owners=("owner",),
        merged_only=True,
        lane_lock_reasons=("lane-a",),
    )
    # A locked stale-and-finished tree with only work on remote: held by the
    # merged-only rule.
    tree = facts(
        worktree_locked=True,
        worktree_lock_reason="lane-a",
        ledger_known=True,
        owner_finished=True,
        head_on_remote=True,
        commits_not_on_remote=0,
        last_activity_age_hours=100,
    )
    assert hold_unmerged(*classify(tree, policy), policy) == (
        Decision.KEEP,
        "merged_only:work_on_remote",
    )


def test_policy_refuses_prune_dead_worktrees() -> None:
    with pytest.raises(ValidationError):
        ModelWorktreeReconcilePolicy(prune_dead_worktrees=True)
