# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A dead worktree is kept and reported; a stale-locked one is unlocked and removed (OMN-22799).

Recorded case: a lane locked its tree with ``git worktree lock`` and left; a later
cleanup removed the clone's worktrees directory out from under other trees. The tick
kept both forever: the locked tree read ``git_locked`` with no claim, process or unit
behind the marker, and the dead tree failed every git command, so its head could not
be read at all. The lock marker and the named gitdir are now facts, so a lock nothing
backs goes stale past ``policy.stale_lock_hours`` and is unlocked before a normal
removal, and a tree whose gitdir is gone is dead junk a person decides, never removed.
Every tree that leaves the disk is named in the completed event, split out by how it
left.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision as Decision,
)
from omnimarket.events.worktree_reconcile import (
    ModelWorktreeReconcileRequest,
    ModelWorktreeReconcileRunCompletedEvent,
)
from omnimarket.handlers.rules_worktree_lock import stale_lock
from omnimarket.models.model_worktree_reconcile_policy import (
    ModelWorktreeReconcilePolicy,
)
from omnimarket.nodes.node_worktree_reconcile_compute.handlers import (
    HandlerWorktreeReconcileCompute,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_facts
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.probe_classes import (
    PROBE_NEEDS_HUMAN_PREFIX,
)
from tests.test_worktree_reconcile_compute import facts
from tests.test_worktree_reconcile_effect import (
    FakeMergedPrReader,
    git_fixture,
    probe_for,
    run_git,
    run_with,
)

pytestmark = pytest.mark.unit

LOCK_REASON = "codex lane running (OMN-20363)"
# The clock run_with ticks at: fixture writes land far in its past.
FUTURE = datetime(2100, 1, 1, tzinfo=UTC)
NESTED = f"{PROBE_NEEDS_HUMAN_PREFIX}nested_repository"
LANE = ModelWorktreeReconcilePolicy(
    allowed_remote_owners=("owner",),
    lane_lock_reasons=(LOCK_REASON,),
)


def test_the_two_knobs_live_in_the_policy_model() -> None:
    policy = ModelWorktreeReconcilePolicy()
    assert policy.stale_lock_hours == 24.0
    assert policy.lane_lock_reasons == ()
    assert policy.ledger_max_age_hours == 6.0
    assert policy.ledger_lookback_days == 3
    with pytest.raises(ValidationError):
        ModelWorktreeReconcilePolicy(prune_dead_worktrees=True)
    # A lookback of 0 would hide a CLAIM that rolled into an archive file.
    with pytest.raises(ValidationError):
        ModelWorktreeReconcilePolicy(ledger_lookback_days=0)


def test_a_lock_is_stale_only_when_nothing_backs_it() -> None:
    assert stale_lock(
        facts(
            worktree_locked=True,
            ledger_known=True,
            worktree_lock_reason=LOCK_REASON,
            owner_finished=True,
            last_activity_age_hours=25.0,
        ),
        LANE,
    )
    base: dict[str, object] = {
        "worktree_locked": True,
        "ledger_known": True,
        "worktree_lock_reason": LOCK_REASON,
        "owner_finished": True,
    }
    for updates in (
        {"ref_locked": True},
        {"live_process": True},
        {"live_claim": True},
        {"live_unit": "onex-hourly.service"},
        {"last_activity_age_hours": 23.9},
        {"last_activity_age_hours": None},
        {"owner_finished": False},
        {"ledger_known": False},
        {"worktree_lock_reason": "held by a person"},
    ):
        merged = {**base, **updates}
        assert not stale_lock(facts(**merged), LANE)
    # No lock marker, nothing to call stale.
    assert not stale_lock(facts(last_activity_age_hours=25.0), LANE)


def test_a_stale_locked_merged_tree_is_removed_and_unlocked() -> None:
    stale = facts(
        worktree_locked=True,
        ledger_known=True,
        git_locked=True,
        content_merged=True,
        worktree_lock_reason=LOCK_REASON,
        owner_finished=True,
    )
    row = (
        HandlerWorktreeReconcileCompute()
        .handle(ModelWorktreeReconcileRequest(facts=(stale,), policy=LANE))
        .decisions[0]
    )
    assert row.decision == Decision.REMOVE
    assert row.reasons == ("content_merged", "stale_lock_unlocked")
    # On the pre-change rule this tree was KEEP git_locked; the stale lock no
    # longer holds it, and the normal merged rules judge the work instead.
    assert row.decision != Decision.KEEP
    assert row.reasons != ("git_locked",)


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"live_claim": True}, "live_claim"),
        ({"last_activity_age_hours": 23.9}, "git_locked"),
        ({"ref_locked": True}, "git_locked"),
    ],
)
def test_a_lock_something_backs_stays_a_keep(
    updates: dict[str, object], reason: str
) -> None:
    held = facts(
        worktree_locked=True,
        git_locked=True,
        content_merged=True,
        worktree_lock_reason=LOCK_REASON,
        owner_finished=True,
        **updates,
    )
    row = (
        HandlerWorktreeReconcileCompute()
        .handle(ModelWorktreeReconcileRequest(facts=(held,), policy=LANE))
        .decisions[0]
    )
    assert (row.decision, row.reasons) == (Decision.KEEP, (reason,))


def test_the_stale_lock_threshold_is_the_policy_knob() -> None:
    stale = facts(
        worktree_locked=True,
        ledger_known=True,
        git_locked=True,
        content_merged=True,
        worktree_lock_reason=LOCK_REASON,
        owner_finished=True,
    )
    patient = ModelWorktreeReconcilePolicy(
        allowed_remote_owners=("owner",),
        lane_lock_reasons=(LOCK_REASON,),
        stale_lock_hours=1000.0,
    )
    row = (
        HandlerWorktreeReconcileCompute()
        .handle(ModelWorktreeReconcileRequest(facts=(stale,), policy=patient))
        .decisions[0]
    )
    assert (row.decision, row.reasons) == (Decision.KEEP, ("git_locked",))


def locked_merged_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A real, clean worktree at the base commit, locked with a lane's reason."""
    clone, root, _ = git_fixture(tmp_path)
    tree = root / "task" / "repo"
    run_git(clone, "worktree", "add", "-b", "feature", str(tree))
    run_git(clone, "worktree", "lock", "--reason", LOCK_REASON, str(tree))
    return clone, root, tree


def test_a_stale_locked_merged_tree_is_unlocked_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, tree = locked_merged_tree(tmp_path)
    ledger = tmp_path / "ledger"
    # The tree's lane closed its claim: a terminal, so no live claim names the tree.
    ledger.write_text(
        "2099-12-31T20:00:00Z | CLAIM | lane=a | ticket=task | running\n"
        "2099-12-31T21:00:00Z | TERMINAL | lane=a | ticket=task | done\n"
        "2099-12-31T23:00:00Z | STATUS | lane=a | ticket=task | later\n"
    )
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger),
        policy=LANE,
    )
    row = result.decided_events[0]
    assert row.facts.worktree_locked
    assert row.facts.ledger_known
    assert row.facts.git_locked
    assert not row.facts.ref_locked
    assert LOCK_REASON in row.facts.worktree_lock_reason
    assert row.decision.reasons == ("content_merged", "stale_lock_unlocked")
    assert row.outcome == "removed"
    assert not tree.exists()
    assert str(tree) not in run_git(clone, "worktree", "list")
    done = result.completed_event
    assert (done.removed, done.failures) == (1, 0)
    assert done.unlocked_paths == (str(tree),)
    assert done.removed_paths == (str(tree),)
    assert not hasattr(done, "pruned")


def test_without_a_ledger_a_locked_tree_stays_and_is_named_live_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ledger read means "no live claim" proves nothing: the lock holds."""
    clone, root, tree = locked_merged_tree(tmp_path)
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        policy=LANE,
        ledger_path=None,
    )
    row = result.decided_events[0]
    assert not row.facts.ledger_known
    assert (row.outcome, row.decision.reasons) == ("kept", ("git_locked",))
    assert result.completed_event.live_locked_paths == (str(tree),)
    assert tree.exists()
    assert str(tree) in run_git(clone, "worktree", "list")


def test_the_same_locked_tree_with_a_live_claim_stays_and_is_named_live_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, tree = locked_merged_tree(tmp_path)
    ledger = tmp_path / "ledger"
    # A claim the run's clock still reads as live, naming the tree's ticket.
    ledger.write_text("2099-12-31T23:00:00Z | CLAIM | lane=a | ticket=task | running\n")
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger),
        policy=LANE,
    )
    row = result.decided_events[0]
    assert row.facts.live_claim
    assert (row.outcome, row.decision.reasons) == ("kept", ("live_claim",))
    done = result.completed_event
    assert done.failures == 0
    assert done.live_locked_paths == (str(tree),)
    assert done.removed_paths == ()
    assert done.unlocked_paths == ()
    assert tree.exists()
    assert str(tree) in run_git(clone, "worktree", "list")


def dead_tree(root: Path, age_hours: float = 100.0, ticket: str = "task") -> Path:
    """A linked worktree whose .git file names a gitdir that no longer exists."""
    tree = root / ticket / "repo"
    tree.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {root.parent / 'gone-worktrees' / ticket}\n")
    (tree / "notes.txt").write_text("leftover\n")
    stamp = (FUTURE - timedelta(hours=age_hours)).timestamp()
    os.utime(tree / "notes.txt", (stamp, stamp))
    return tree


def test_a_dead_tree_past_the_stale_age_waits_for_a_person_and_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root, _ = git_fixture(tmp_path)
    tree = dead_tree(root)
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        policy=LANE,
    )
    row = result.decided_events[0]
    assert row.facts.gitdir_missing
    assert row.facts.probe_errors == ()
    assert row.facts.file_names == ("notes.txt",)
    assert (row.outcome, row.decision.reasons) == (
        "needs_human",
        ("gitdir_unreadable",),
    )
    done = result.completed_event
    assert (done.failures, done.needs_human) == (0, 1)
    assert done.needs_human_paths == (str(tree),)
    assert done.removed_paths == ()
    assert tree.exists()
    assert not (root / ".onex_state" / "worktree-reconcile-trash").exists()


def test_without_the_prune_knob_a_dead_tree_waits_for_a_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root, _ = git_fixture(tmp_path)
    tree = dead_tree(root)
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        policy=LANE,
    )
    row = result.decided_events[0]
    assert row.facts.gitdir_missing
    assert (row.outcome, row.decision.reasons) == (
        "needs_human",
        ("gitdir_unreadable",),
    )
    done = result.completed_event
    assert (done.failures, done.needs_human) == (0, 1)
    assert done.needs_human_paths == (str(tree),)
    assert done.removed_paths == ()
    assert tree.exists()


def test_a_dead_tree_newer_than_the_stale_age_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root, _ = git_fixture(tmp_path)
    tree = dead_tree(root, age_hours=24.0)  # quiet, but younger than stale_hours
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        policy=LANE,
    )
    row = result.decided_events[0]
    assert (row.outcome, row.decision.reasons) == ("kept", ("not_stale",))
    assert result.completed_event.removed_paths == ()
    assert tree.exists()


def test_a_dead_tree_a_process_works_in_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root, _ = git_fixture(tmp_path)
    tree = dead_tree(root)
    probe = probe_for(FakeMergedPrReader(), monkeypatch)
    monkeypatch.setattr(adapter_facts, "process_cwds", lambda: (tree.resolve(),))
    result = run_with(probe, root, monkeypatch, execute=True, policy=LANE)
    row = result.decided_events[0]
    assert row.facts.live_process
    assert (row.outcome, row.decision.reasons) == ("kept", ("live_process",))
    assert result.completed_event.removed_paths == ()
    assert tree.exists()


def test_a_dead_tree_holding_a_nested_repository_goes_to_a_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root, _ = git_fixture(tmp_path)
    tree = dead_tree(root)
    run_git(tmp_path, "init", "--quiet", str(tree / "inner"))
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        policy=LANE,
    )
    row = result.decided_events[0]
    assert row.facts.probe_errors == (NESTED,)
    assert (row.outcome, row.decision.reasons) == (
        "needs_human",
        ("nested_repository",),
    )
    done = result.completed_event
    assert done.failures == 0
    assert done.removed_paths == ()
    assert tree.exists()


def test_a_merged_only_host_never_removes_a_dead_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root, _ = git_fixture(tmp_path)
    tree = dead_tree(root)
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        policy=ModelWorktreeReconcilePolicy(
            allowed_remote_owners=("owner",),
            merged_only=True,
            lane_lock_reasons=(LOCK_REASON,),
        ),
    )
    row = result.decided_events[0]
    assert row.outcome == "needs_human"
    assert result.completed_event.removed_paths == ()
    assert tree.exists()


def test_an_old_receipt_without_the_new_columns_still_loads() -> None:
    old = json.dumps(
        {
            "correlation_id": "00000000-0000-0000-0000-000000000001",
            "host": "host",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T01:00:00Z",
            "scanned": 3,
            "removed": 1,
            "pinned_and_removed": 0,
            "kept": 1,
            "needs_human": 1,
            "failures": 0,
            "freed_bytes": 10,
            "needs_human_paths": ["trees/task/repo"],
            "deferred": 1,
            "deferred_paths": ["trees/task/slow"],
        }
    )
    loaded = ModelWorktreeReconcileRunCompletedEvent.model_validate_json(old)
    assert (
        loaded.removed_paths,
        loaded.unlocked_paths,
        loaded.live_locked_paths,
    ) == ((), (), ())
