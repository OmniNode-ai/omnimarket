"""Git branch probe — scans omni_home/worktrees for active worktree branches."""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelGitBranchSnapshot,
    ProbeSnapshotItem,
)

logger = logging.getLogger(__name__)

_WORKTREES_SUBDIR = "worktrees"


def _get_branch_age_days(worktree_path: Path) -> float:
    """Return age of the current branch HEAD in days via git log."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree_path), "log", "-1", "--format=%ct"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return 0.0
        commit_ts = int(proc.stdout.strip())
        now_ts = datetime.now(UTC).timestamp()
        return round((now_ts - commit_ts) / 86400, 2)
    except Exception:
        return 0.0


def _get_current_branch(worktree_path: Path) -> str | None:
    """Return the current branch name for a worktree, or None if detached."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return None
        branch = proc.stdout.strip()
        return branch if branch != "HEAD" else None
    except Exception:
        return None


class ProbeGitBranches:
    """Probe that scans omni_home/worktrees/ for active feature branches."""

    name: str = "git_branches"

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        """Scan worktrees directory and collect branch snapshots.

        Returns empty list on any failure — probe errors are non-fatal.
        """
        worktrees_root = Path(omni_home).parent.parent / "omni_worktrees"
        if not worktrees_root.exists():
            # Try the sibling path pattern used in the platform
            worktrees_root = Path("/Volumes/PRO-G40/Code/omni_worktrees")
        if not worktrees_root.exists():
            logger.warning("Worktrees directory not found at %s", worktrees_root)
            return []

        results: list[ProbeSnapshotItem] = []

        # Each ticket dir has one or more repo subdirs
        for ticket_dir in sorted(worktrees_root.iterdir()):
            if not ticket_dir.is_dir():
                continue
            for repo_dir in sorted(ticket_dir.iterdir()):
                if not repo_dir.is_dir():
                    continue
                git_dir = repo_dir / ".git"
                if not git_dir.exists():
                    continue

                branch = _get_current_branch(repo_dir)
                if branch is None:
                    continue

                age_days = _get_branch_age_days(repo_dir)
                results.append(
                    ModelGitBranchSnapshot(
                        repo=repo_dir.name,
                        branch=branch,
                        worktree_path=str(repo_dir),
                        age_days=age_days,
                    )
                )

        return results


__all__: list[str] = ["ProbeGitBranches"]
