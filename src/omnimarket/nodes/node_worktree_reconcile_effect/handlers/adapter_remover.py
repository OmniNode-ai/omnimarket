# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Non-forced linked removal and same-filesystem standalone trash."""

import contextlib
import os
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from omnimarket.events.worktree_reconcile import EnumWorktreeKind as Kind
from omnimarket.events.worktree_reconcile import ModelWorktreeFacts
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_commands import (
    git,
)


class GitWorktreeRemover:
    def remove(self, facts: ModelWorktreeFacts, now: datetime) -> None:
        path, root = Path(facts.path), Path(facts.root)
        if (
            path.is_symlink()
            or path.resolve() == root.resolve()
            or not path.resolve().is_relative_to(root.resolve())
        ):
            raise ValueError("unsafe removal path")
        if facts.kind == Kind.LINKED_WORKTREE:
            if not (path / ".git").is_file():
                raise ValueError("linked worktree identity changed")
            if facts.gitdir_missing:
                raise ValueError("gitdir unreadable")
            common = git(
                str(path),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ).stdout.strip()
            if facts.worktree_locked:
                # A stale lock would refuse the removal; the rules proved
                # nothing live holds the tree, so the marker goes first.
                git(common, "worktree", "unlock", str(path))
            try:
                git(common, "worktree", "remove", str(path))
            except Exception:
                if facts.worktree_locked:
                    # The unlock above must not outlive a removal that failed: put the lock
                    # back with its reason. A failure of the relock never hides the original.
                    relock = ["worktree", "lock"]
                    if facts.worktree_lock_reason:
                        relock += ["--reason", facts.worktree_lock_reason]
                    with contextlib.suppress(Exception):
                        git(common, *relock, str(path))
                raise
            ticket = path.parent
            if (
                ticket != root
                and ticket.is_relative_to(root)
                and not any(ticket.iterdir())
            ):
                ticket.rmdir()
        elif facts.kind == Kind.STANDALONE_CLONE:
            if (
                not (path / ".git").is_dir()
                or facts.dirty
                or not facts.head_on_remote
                or not facts.local_branches_all_on_remote
            ):
                raise ValueError("clone preservation not proven")
            self._trash(path, root, now)
        else:
            path.rmdir()

    @staticmethod
    def _trash(path: Path, root: Path, now: datetime) -> None:
        """Move a tree into the root's same-filesystem trash, then delete it."""
        trash = (
            root / ".onex_state" / "worktree-reconcile-trash" / now.strftime("%Y-%m-%d")
        )
        # Never follow a replacement of the trash parent outside the root.
        if not trash.resolve().is_relative_to(
            root.resolve()
        ) or trash.resolve().is_relative_to(path.resolve()):
            raise ValueError("unsafe trash path")
        trash.mkdir(parents=True, exist_ok=True)
        if path.stat().st_dev != trash.stat().st_dev:
            raise ValueError("trash must be on the same filesystem")
        destination = trash / f"{path.name}-{uuid4().hex}"
        os.rename(path, destination)
        shutil.rmtree(destination)
