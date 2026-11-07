# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pin history backends: whether each pinned omnimarket revision is on the merged branch.

A pin to a squashed branch commit names a revision the merged branch never holds; the overlay
may still load, but its pin points at code that did not land. The in-memory backend is the
default for tests; the git backend asks an omnimarket clone with ``git merge-base
--is-ancestor``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.nodes.node_overlay_drift_effect.protocols import PinHistoryError

Runner = Callable[[Sequence[str], int], "subprocess.CompletedProcess[str]"]


class InMemoryPinHistory:
    """The merged revisions are given; any other revision is unmerged."""

    def __init__(self, merged: frozenset[str]) -> None:
        self.merged = merged

    def unmerged(self, revisions: tuple[str, ...]) -> frozenset[str]:
        return frozenset(r for r in revisions if r not in self.merged)


def run_git(command: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command), capture_output=True, text=True, check=False, timeout=timeout
    )


class GitPinHistory:
    """Ask an omnimarket clone whether each revision is an ancestor of its merged ref."""

    def __init__(
        self, clone: Path, merged_ref: str, timeout_seconds: int, run: Runner = run_git
    ) -> None:
        self.clone = clone
        self.merged_ref = merged_ref
        self.timeout_seconds = timeout_seconds
        self.run = run

    def _is_merged(self, revision: str) -> bool:
        try:
            done = self.run(
                [
                    "git",
                    "-C",
                    str(self.clone),
                    "merge-base",
                    "--is-ancestor",
                    revision,
                    self.merged_ref,
                ],
                self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PinHistoryError(f"git-{type(exc).__name__}") from None
        if done.returncode == 0:
            return True
        if done.returncode == 1:
            return False
        # 128: an unknown revision (never fetched, or gone) is not on the merged branch either,
        # but an unknown merged ref means the clone itself cannot answer.
        verified = self.run(
            [
                "git",
                "-C",
                str(self.clone),
                "rev-parse",
                "--verify",
                "--quiet",
                self.merged_ref,
            ],
            self.timeout_seconds,
        )
        if verified.returncode:
            raise PinHistoryError("merged-ref-unknown")
        return False

    def unmerged(self, revisions: tuple[str, ...]) -> frozenset[str]:
        if not (self.clone / ".git").exists():
            raise PinHistoryError("clone-absent")
        return frozenset(r for r in dict.fromkeys(revisions) if not self._is_merged(r))


__all__ = ["GitPinHistory", "InMemoryPinHistory", "run_git"]
