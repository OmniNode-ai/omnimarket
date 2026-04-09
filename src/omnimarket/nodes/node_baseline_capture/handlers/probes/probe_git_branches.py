"""ProbeGitBranches — snapshot worktree branches under omni_worktrees."""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelGitBranchSnapshot,
)

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 10
_WORKTREES_ROOT: str = "/Volumes/PRO-G40/Code/omni_worktrees"


def _branch_age_days(worktree_path: Path) -> float:
    """Return approximate branch age in days via git log --format=%ct on HEAD."""
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "log", "-1", "--format=%ct"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return 0.0
        commit_ts = int(result.stdout.strip())
        now_ts = int(datetime.now(UTC).timestamp())
        return round(max(0.0, (now_ts - commit_ts) / 86400), 2)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 0.0


def _current_branch(worktree_path: Path) -> str | None:
    """Return the current branch name of a git worktree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        return branch if branch and branch != "HEAD" else None
    except (subprocess.TimeoutExpired, OSError):
        return None


class ProbeGitBranches:
    """Scan omni_worktrees for active git worktree branches."""

    name: str = "git_branches"

    async def collect(
        self, omni_home: str = "/Volumes/PRO-G40/Code/omni_home"
    ) -> list[ModelGitBranchSnapshot]:
        """Return branch snapshots for all discovered worktrees; never raises."""
        worktrees_root = Path(_WORKTREES_ROOT)
        if not worktrees_root.exists():
            logger.warning(
                "probe_git_branches: worktrees root not found: %s", worktrees_root
            )
            return []

        snapshots: list[ModelGitBranchSnapshot] = []

        # Scan two levels deep: worktrees_root/<ticket>/<repo>/
        for ticket_dir in sorted(worktrees_root.iterdir()):
            if not ticket_dir.is_dir():
                continue
            for repo_dir in sorted(ticket_dir.iterdir()):
                if not repo_dir.is_dir():
                    continue
                git_dir = repo_dir / ".git"
                if not git_dir.exists():
                    continue

                branch = _current_branch(repo_dir)
                if branch is None:
                    continue

                age_days = _branch_age_days(repo_dir)

                try:
                    snapshots.append(
                        ModelGitBranchSnapshot(
                            repo=repo_dir.name,
                            branch=branch,
                            worktree_path=str(repo_dir),
                            age_days=age_days,
                        )
                    )
                except Exception as exc:
                    logger.warning("probe_git_branches: skipping %s: %s", repo_dir, exc)

        return snapshots


__all__: list[str] = ["ProbeGitBranches"]
