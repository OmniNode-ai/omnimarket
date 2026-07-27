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
# Upper bound on the per-path history walk used to prove a behind-the-target
# worktree is recoverable. Bounded work in the merge tail; exceeding it reports
# "not found", which preserves the worktree (OMN-15251).
_MAX_HISTORY_COMMITS = 200


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

    def content_sha_at_ref(
        self, worktree_path: str, ref: str, rel_path: str
    ) -> str | None:
        """Blob hash for ``rel_path`` at ``ref``, or None when absent there.

        ``git rev-parse <ref>:<path>`` yields git's own object id, which is the
        same function of content that ``hash-object`` computes for the working
        file — so the two are directly comparable. A non-zero exit means the
        path does not exist at that ref (OMN-15251).
        """
        proc = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--verify", f"{ref}:{rel_path}"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def working_content_sha(self, worktree_path: str, rel_path: str) -> str | None:
        """Blob hash of the working-tree file, or None when it is absent."""
        if not (Path(worktree_path) / rel_path).is_file():
            return None
        proc = subprocess.run(
            ["git", "-C", worktree_path, "hash-object", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def content_sha_in_ref_history(
        self, worktree_path: str, ref: str, rel_path: str, content_sha: str
    ) -> bool:
        """True when ``content_sha`` is any historical version of ``rel_path`` on ``ref``.

        Implemented as ``git rev-list <ref> -- <path>`` (commits that touched the
        path) piped through ``git rev-parse <commit>:<path>``. Bounded by
        ``_MAX_HISTORY_COMMITS`` so a long-lived file cannot stall the merge
        tail; exhausting the bound returns False, which preserves the worktree
        (OMN-15251 fails closed).
        """
        listing = subprocess.run(
            [
                "git",
                "-C",
                worktree_path,
                "rev-list",
                f"--max-count={_MAX_HISTORY_COMMITS}",
                ref,
                "--",
                rel_path,
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if listing.returncode != 0:
            return False
        for commit in listing.stdout.split():
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    worktree_path,
                    "rev-parse",
                    "--verify",
                    f"{commit}:{rel_path}",
                ],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip() == content_sha:
                return True
        return False

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
