# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitWorktreeAdapter — real ``git`` implementation of ProtocolGitWorktreeAdapter.

Thin subprocess wrapper. All decision logic (path safety, dirty classification)
lives in ``HandlerWorktreePrune``; this adapter only shells out to git.

Related:
    - OMN-13859: Event-driven worktree prune-on-PR-close.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 60


class GitWorktreeAdapter:
    """Executes git worktree operations via subprocess."""

    def status_porcelain(self, worktree_path: str) -> str:
        proc = subprocess.run(
            ["git", "-C", worktree_path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=True,
        )
        return proc.stdout

    def git_common_dir(self, worktree_path: str) -> str:
        proc = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=True,
        )
        raw = proc.stdout.strip()
        common = Path(raw)
        # git may return the common dir relative to the worktree; make absolute.
        if not common.is_absolute():
            common = (Path(worktree_path) / common).resolve()
        return str(common)

    def worktree_remove(self, canonical_root: str, worktree_path: str) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                canonical_root,
                "worktree",
                "remove",
                "--force",
                worktree_path,
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=True,
        )


__all__: list[str] = ["GitWorktreeAdapter"]
