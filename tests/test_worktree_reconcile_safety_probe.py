# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Locked merged trees are removed only when the lane lock is provably stale (OMN-777).

A lane locks its tree with ``git worktree lock`` and leaves. The reconcile tick must
not remove a tree a live lane still holds, and must not lose work a lane left behind:

- a dry run decides but touches nothing on disk;
- a lock reason that names a live lane (``policy.lane_lock_reasons``) is never stale;
- a live claim on the ticket in the ledger keeps the tree;
- a ledger whose newest row is older than ``policy.ledger_max_age_hours`` proves
  nothing, so every tree is deferred and nothing is removed;
- uncommitted or unpushed work is never removed;
- a failed ``worktree remove`` restores the lock so the next tick can retry;
- a tree whose gitdir is gone is dead junk a person decides, never removed.

Each case carries a CONTROL (the same setup differing in one thing) asserting the
opposite outcome, so a regression that flips one side is caught by the other.
"""

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision as Decision,
)
from omnimarket.models.model_worktree_reconcile_policy import (
    ModelWorktreeReconcilePolicy,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_remover
from tests.test_worktree_reconcile_effect import (
    FakeMergedPrReader,
    git_fixture,
    probe_for,
    run_git,
    run_with,
)

pytestmark = pytest.mark.unit

LOCK_REASON = "codex lane running (OMN-777)"
TICKET = "OMN-777"
# The clock run_with ticks at: fixture writes land far in its past.
FUTURE = datetime(2100, 1, 1, tzinfo=UTC)
POLICY = ModelWorktreeReconcilePolicy(
    allowed_remote_owners=("owner",),
    lane_lock_reasons=(LOCK_REASON,),
    stale_lock_hours=1,
    quiet_hours=0,
)


def _stamp(hours_ago: float) -> str:
    """A UTC stamp ``hours_ago`` before the 2100 clock the run ticks at."""
    return (FUTURE - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finished_ledger(tmp_path: Path, *, hours_ago: float = 1.0) -> Path:
    """A ledger whose lane closed its claim and whose newest row is recent."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    ledger = tmp_path / "ledger"
    ledger.write_text(
        f"{_stamp(hours_ago)} | CLAIM | lane=a | ticket={TICKET} | worktree=omni_worktrees/OMN-777/repo\n"
        f"{_stamp(hours_ago - 1.0)} | TERMINAL | lane=a | ticket={TICKET} | worktree=omni_worktrees/OMN-777/repo\n"
        f"{_stamp(0.5)} | STATUS | lane=a | ticket={TICKET} | worktree=omni_worktrees/OMN-777/repo\n"
    )
    return ledger


def _locked_tree(
    tmp_path: Path, *, reason: str = LOCK_REASON
) -> tuple[Path, Path, Path]:
    """A merged, pushed, clean linked worktree locked with ``reason``."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    clone, root, _ = git_fixture(tmp_path)
    tree = root / TICKET / "repo"
    run_git(clone, "worktree", "add", "-b", "feature", str(tree))
    (tree / "landed").write_text("landed\n")
    run_git(tree, "add", "landed")
    run_git(tree, "commit", "-m", "landed")
    run_git(clone, "merge", "feature")
    run_git(clone, "push", "origin", "main")
    run_git(clone, "worktree", "lock", "--reason", reason, str(tree))
    return clone, root, tree


def _lock_file(tree: Path) -> Path:
    """The ``locked`` file in the tree's gitdir, holding the lock reason."""
    gitdir = (tree / ".git").read_text().splitlines()[0].split("gitdir: ")[1].strip()
    return Path(gitdir) / "locked"


def test_dry_run_decides_without_touching_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, tree = _locked_tree(tmp_path)
    ledger = _finished_ledger(tmp_path)
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=False,
        ledger_path=str(ledger),
        policy=POLICY,
    )
    row = result.decided_events[0]
    assert row.decision.decision == Decision.REMOVE
    assert row.outcome == "dry_run"
    assert tree.exists()
    assert _lock_file(tree).exists()
    assert str(tree) in run_git(clone, "worktree", "list")
    done = result.completed_event
    assert (done.removed, done.failures) == (0, 0)
    assert done.removed_paths == ()
    # CONTROL: the same setup that executes removes the tree.
    _clone2, root2, tree2 = _locked_tree(tmp_path / "control")
    ledger2 = _finished_ledger(tmp_path / "control")
    result2 = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root2,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger2),
        policy=POLICY,
    )
    assert result2.decided_events[0].outcome == "removed"
    assert not tree2.exists()


def test_a_person_lock_is_kept_and_a_lane_lock_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, tree = _locked_tree(tmp_path, reason="held by a person")
    ledger = _finished_ledger(tmp_path)
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger),
        policy=POLICY,
    )
    row = result.decided_events[0]
    assert row.outcome == "kept"
    assert row.decision.reasons == ("git_locked",)
    assert tree.exists()
    assert _lock_file(tree).exists()
    assert str(tree) in run_git(clone, "worktree", "list")
    # CONTROL: the same setup with the lane's lock reason is removed.
    _clone2, root2, tree2 = _locked_tree(tmp_path / "control", reason=LOCK_REASON)
    ledger2 = _finished_ledger(tmp_path / "control")
    result2 = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root2,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger2),
        policy=POLICY,
    )
    assert result2.decided_events[0].outcome == "removed"
    assert not tree2.exists()


def test_a_live_successor_claim_keeps_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, tree = _locked_tree(tmp_path)
    ledger = tmp_path / "ledger"
    ledger.write_text(
        f"{_stamp(1.0)} | CLAIM | lane=a | ticket={TICKET} | worktree=omni_worktrees/OMN-777/repo\n"
        f"{_stamp(0.5)} | CLAIM | lane=b | ticket={TICKET} | worktree=omni_worktrees/OMN-777/repo\n"
        f"{_stamp(0.25)} | STATUS | lane=b | ticket={TICKET} | worktree=omni_worktrees/OMN-777/repo\n"
    )
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger),
        # A claim is live while its lane has written within quiet_hours.
        policy=POLICY.model_copy(update={"quiet_hours": 6.0}),
    )
    row = result.decided_events[0]
    assert row.facts.live_claim
    assert row.outcome == "kept"
    assert row.decision.reasons == ("live_claim",)
    assert tree.exists()
    assert str(tree) in run_git(clone, "worktree", "list")
    # CONTROL: no successor claim, so the finished lane's tree is removed.
    _clone2, root2, tree2 = _locked_tree(tmp_path / "control")
    ledger2 = _finished_ledger(tmp_path / "control")
    result2 = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root2,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger2),
        policy=POLICY,
    )
    assert result2.decided_events[0].outcome == "removed"
    assert not tree2.exists()


def test_a_lagging_ledger_reads_unread_and_keeps_every_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, tree = _locked_tree(tmp_path)
    ledger = tmp_path / "ledger"
    # Newest row 7 hours before the 2100 clock; the policy allows 6.
    ledger.write_text(
        f"{_stamp(7.0)} | CLAIM | lane=a | ticket={TICKET} | worktree=omni_worktrees/OMN-777/repo\n"
        f"{_stamp(7.5)} | TERMINAL | lane=a | ticket={TICKET} | worktree=omni_worktrees/OMN-777/repo\n"
    )
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger),
        policy=POLICY.model_copy(update={"ledger_max_age_hours": 6.0}),
    )
    done = result.completed_event
    # A frozen ledger is unread, not deferred: waiting does not read it, so the run
    # says so (a failure a person must see) and every tree stays.
    assert done.ledger_unread == "frozen"
    assert result.errors == ("ledger_unread:frozen",)
    assert done.failures == 1
    assert done.removed == 0
    assert done.removed_paths == ()
    assert done.deferred == 0
    assert result.decided_events[0].decision.reasons == ("git_locked",)
    assert tree.exists()
    assert str(tree) in run_git(clone, "worktree", "list")
    # CONTROL: a fresh ledger row removes the tree.
    _clone2, root2, tree2 = _locked_tree(tmp_path / "control")
    ledger2 = _finished_ledger(tmp_path / "control")
    result2 = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root2,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger2),
        policy=POLICY.model_copy(update={"ledger_max_age_hours": 6.0}),
    )
    assert result2.decided_events[0].outcome == "removed"
    assert not tree2.exists()
    # An empty ledger file proves nothing: everything is kept.
    _clone3, root3, tree3 = _locked_tree(tmp_path / "empty")
    ledger3 = tmp_path / "empty" / "ledger"
    ledger3.write_text("")
    result3 = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root3,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger3),
        policy=POLICY,
    )
    assert result3.completed_event.removed == 0
    assert tree3.exists()


def test_uncommitted_work_is_kept_and_clean_work_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, tree = _locked_tree(tmp_path)
    (tree / "tracked").write_text("unsaved\n")
    ledger = _finished_ledger(tmp_path)
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger),
        policy=POLICY,
    )
    row = result.decided_events[0]
    # Past the stale age a dirty tree goes to a person; it is never removed.
    assert (row.outcome, row.decision.reasons) == ("needs_human", ("dirty",))
    assert tree.exists()
    assert str(tree) in run_git(clone, "worktree", "list")
    # CONTROL: a clean merged tree is removed.
    _clone2, root2, tree2 = _locked_tree(tmp_path / "control")
    ledger2 = _finished_ledger(tmp_path / "control")
    result2 = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root2,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger2),
        policy=POLICY,
    )
    assert result2.decided_events[0].outcome == "removed"
    assert not tree2.exists()


def test_an_unpushed_branch_is_never_pinned_or_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, tree = _locked_tree(tmp_path)
    (tree / "unpushed").write_text("unpushed\n")
    run_git(tree, "add", "unpushed")
    run_git(tree, "commit", "-m", "unpushed")
    ledger = _finished_ledger(tmp_path)
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger),
        policy=POLICY,
    )
    row = result.decided_events[0]
    assert row.outcome == "kept"
    assert tree.exists()
    assert str(tree) in run_git(clone, "worktree", "list")
    # CONTROL: a pushed merged tree is removed.
    _clone2, root2, tree2 = _locked_tree(tmp_path / "control")
    ledger2 = _finished_ledger(tmp_path / "control")
    result2 = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root2,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger2),
        policy=POLICY,
    )
    assert result2.decided_events[0].outcome == "removed"
    assert not tree2.exists()


def test_a_failed_remove_restores_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, root, tree = _locked_tree(tmp_path)
    lock = _lock_file(tree)
    original_reason = lock.read_text()
    ledger = _finished_ledger(tmp_path)
    real_git = adapter_remover.git

    def failing_git(path: str, *args: str, **kwargs: object) -> object:
        if args[:2] == ("worktree", "remove"):
            raise subprocess.CalledProcessError(1, ["git", "worktree", "remove"])
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(adapter_remover, "git", failing_git)
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger),
        policy=POLICY,
    )
    row = result.decided_events[0]
    assert row.outcome == "failed"
    assert tree.exists()
    assert lock.exists()
    assert lock.read_text() == original_reason
    assert str(tree) in run_git(clone, "worktree", "list")
    # CONTROL: without the failure the tree is removed.
    monkeypatch.setattr(adapter_remover, "git", real_git)
    _clone2, root2, tree2 = _locked_tree(tmp_path / "control")
    ledger2 = _finished_ledger(tmp_path / "control")
    result2 = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root2,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger2),
        policy=POLICY,
    )
    assert result2.decided_events[0].outcome == "removed"
    assert not tree2.exists()


def test_a_dead_tree_is_never_removed_and_waits_for_a_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clone, root, tree = _locked_tree(tmp_path)
    # Delete the worktree's gitdir: the .git file now names a gitdir that is gone.
    gitdir = (tree / ".git").read_text().splitlines()[0].split("gitdir: ")[1].strip()
    import shutil

    shutil.rmtree(Path(gitdir))
    ledger = _finished_ledger(tmp_path)
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger),
        policy=POLICY.model_copy(update={"merged_only": False}),
    )
    row = result.decided_events[0]
    assert row.facts.gitdir_missing
    assert row.outcome == "needs_human"
    assert row.decision.reasons == ("gitdir_unreadable",)
    assert tree.exists()
    done = result.completed_event
    assert done.removed == 0
    assert done.failures == 0
    assert done.needs_human >= 1


def test_a_tree_the_ledger_never_names_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh ledger that holds no closed claim for the tree does not show its owner finished."""
    clone, root, tree = _locked_tree(tmp_path)
    ledger = tmp_path / "ledger"
    ledger.write_text(
        f"{_stamp(1.0)} | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        f"{_stamp(0.5)} | TERMINAL | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
    )
    result = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger),
        policy=POLICY,
    )
    row = result.decided_events[0]
    assert row.facts.ledger_known
    assert not row.facts.owner_finished
    assert (row.outcome, row.decision.reasons) == ("kept", ("git_locked",))
    assert tree.exists()
    assert str(tree) in run_git(clone, "worktree", "list")
    # CONTROL: the ledger names the tree and shows its lane finished: removed.
    _clone2, root2, tree2 = _locked_tree(tmp_path / "control")
    ledger2 = _finished_ledger(tmp_path / "control")
    result2 = run_with(
        probe_for(FakeMergedPrReader(), monkeypatch),
        root2,
        monkeypatch,
        execute=True,
        ledger_path=str(ledger2),
        policy=POLICY,
    )
    assert result2.decided_events[0].outcome == "removed"
    assert not tree2.exists()
