# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lock, dead-tree and removal-report fields of the worktree reconcile events (OMN-24560).

They live here, not in ``omnimarket.events.worktree_reconcile``, which is a baselined file that
only shrinks: the facts and the run-completed event inherit them.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

WorktreeOutcome = Literal[
    "dry_run",
    "removed",
    "pinned_and_removed",
    "kept",
    "needs_human",
    "failed",
]


class ModelWorktreeLockFacts(BaseModel):
    """What the probe read of a tree's lock markers and of a missing gitdir."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The tree's own lock marker file (`locked` in its gitdir) and that file's
    # text: the lane that wrote it says why it holds the tree. A lock is stale
    # only when its reason is a policy.lane_lock_reasons entry, the ledger shows
    # the owner finished, no process, unit or ref lock backs it and the tree is
    # quiet past policy.stale_lock_hours.
    worktree_locked: bool = False
    worktree_lock_reason: str = ""
    # A fresh bounded read of the ledger just succeeded (a path merely named, or an
    # empty, rolled or frozen file, is not a read): only then is "no live claim names
    # this tree" evidence, so only then can a lock be called stale.
    ledger_known: bool = False
    # That fresh read maps this tree to at least one lane CLAIM with a later TERMINAL
    # and no open CLAIM names it: the owning lane is shown finished. (A live successor CLAIM on the
    # tree's ticket is read as live_claim, not here.)
    owner_finished: bool = False
    # A transient git lock (index.lock or HEAD.lock): a command is running or
    # died mid-write, so the lock is never called stale.
    ref_locked: bool = False
    # The tree's .git file names a gitdir that no longer exists: git cannot
    # read the tree at all, so its head is dead work left on the disk.
    gitdir_missing: bool = False


class ModelWorktreeRemovalReport(BaseModel):
    """Every removal a run made, named in the run-completed event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Every tree that left the disk (removed or pinned_and_removed), in
    # decision order, so a receipt names each removal. The narrower lists say
    # how it left: unlocked names the stale-locked trees that were unlocked
    # before removal.
    removed_paths: tuple[str, ...] = ()
    unlocked_paths: tuple[str, ...] = ()
    # Trees whose worktree lock is not stale (a live lane, process, unit or ref
    # lock backs it, or the tree is still quiet only briefly): kept and named.
    live_locked_paths: tuple[str, ...] = ()
