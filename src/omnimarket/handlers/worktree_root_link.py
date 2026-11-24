# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registry root's ``omni_worktrees`` resolves to the lab host row's declared worktree root (OMN-26371).

A lane's worktree is made under ``<registry root>/omni_worktrees``, which sits on the boot disk unless something routes it
to the data drive. The route is one symlink, written here from the row's ``lab_worktree_root`` and from nothing else. It is
never written over a directory a lane holds: a directory with entries is reported ``held`` and left as it is, and a link
that already names another place is refused, so the install never moves or hides a tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omnimarket.handlers.work_root_volume_bar import (
    VolumeReader,
    WorkRootVolumeError,
    refuse_work_root_over_bar,
)

WORKTREES_DIRECTORY = "omni_worktrees"


class WorktreeRootRefusalError(Exception):
    """The declared worktree root cannot be used; ``reason`` and ``code`` are the typed refusal."""

    def __init__(self, code: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class WorktreeRootPlan:
    """What the install does about the registry root's worktree directory."""

    declared: Path | None
    link: Path
    action: str  # undeclared | already-linked | link | held
    held_entries: int = 0

    def state(self, *, dry_run: bool) -> str:
        if self.action == "held":
            return f"held: {self.held_entries} entries under the registry {WORKTREES_DIRECTORY}"
        if self.action == "link":
            return "would-link" if dry_run else "linked"
        return self.action


def plan_worktree_root(
    registry_root: str, declared: str | None, *, volume: VolumeReader
) -> WorktreeRootPlan:
    """Read the registry root and the declared root; write nothing."""
    link = Path(registry_root) / WORKTREES_DIRECTORY
    if not declared:
        return WorktreeRootPlan(None, link, "undeclared")
    target = Path(declared)
    try:
        refuse_work_root_over_bar(target, reader=volume)
    except WorkRootVolumeError as exc:
        raise WorktreeRootRefusalError(
            75, "work-root-volume-over-bar", str(exc)
        ) from exc
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return WorktreeRootPlan(target, link, "already-linked")
        raise WorktreeRootRefusalError(
            2,
            "worktree-root-link-elsewhere",
            f"{link} is a link to {link.resolve()}, not the declared {target}; move its trees and remove the link first",
        )
    if link.is_dir():
        entries = sum(1 for _ in link.iterdir())
        if entries:
            return WorktreeRootPlan(target, link, "held", entries)
    return WorktreeRootPlan(target, link, "link")


def apply_worktree_root(plan: WorktreeRootPlan) -> bool:
    """Make the declared root and point the registry link at it; True when it wrote anything."""
    if plan.action != "link" or plan.declared is None:
        return False
    plan.declared.mkdir(parents=True, exist_ok=True)
    if plan.link.is_dir():
        plan.link.rmdir()
    plan.link.symlink_to(plan.declared)
    return True
