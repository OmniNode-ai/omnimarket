# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitWorktreeAdapter — real ``git`` implementation of ProtocolGitWorktreeAdapter.

Thin subprocess wrapper. All decision logic (path safety, dirty classification)
lives in ``HandlerWorktreePrune``; this adapter only shells out to git.

Related:
    - OMN-13859: Event-driven worktree prune-on-PR-close.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 60
# Upper bound on the per-path history walk used to prove a behind-the-target
# worktree is recoverable. Bounded work in the merge tail; exceeding it reports
# "not found", which preserves the worktree (OMN-15251).
_MAX_HISTORY_COMMITS = 200

# The one pre-removal save every worktree-removal path calls (OMN-19539). It
# lives in omniclaude, resolved under the registry root; stdlib only, so any
# interpreter runs it.
_SNAPSHOT_HELPER_REL = Path("omniclaude") / "scripts" / "worktree_removal_snapshot.py"
_SNAPSHOT_TIMEOUT_SECONDS = 1800


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

    def snapshot_before_removal(self, worktree_path: str) -> str:
        """Save the worktree through the shared helper; return the snapshot dir.

        Fails closed: an unset ``OMNI_HOME``, a missing helper, a non-zero exit
        or unreadable output raises, and the handler keeps the worktree.
        """
        registry = os.environ.get("OMNI_HOME")
        if not registry:
            raise RuntimeError(
                "OMNI_HOME is not set, so there is nowhere to save the worktree"
            )
        helper = Path(registry) / _SNAPSHOT_HELPER_REL
        if not helper.is_file():
            raise RuntimeError(f"pre-removal snapshot helper missing: {helper}")
        proc = subprocess.run(
            [
                sys.executable,
                str(helper),
                worktree_path,
                "--reason",
                "pr_lifecycle_worktree_prune",
            ],
            capture_output=True,
            text=True,
            timeout=_SNAPSHOT_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"pre-removal snapshot failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:400]}"
            )
        directory = str(json.loads(proc.stdout)["directory"])
        if not Path(directory).is_dir():
            raise RuntimeError(f"snapshot directory missing on disk: {directory}")
        return directory


__all__: list[str] = ["GitWorktreeAdapter"]
