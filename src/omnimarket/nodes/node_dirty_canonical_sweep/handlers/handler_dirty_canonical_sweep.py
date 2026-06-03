# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Auto-ship dirty canonical repo clones to worktrees and PRs.

Safety invariants:
- Never commits to the canonical repo.
- Restores the canonical repo to clean state via `git checkout -- .` only
  after the dirty files have been copied to the worktree and committed there.
- Creates at most one worktree per dirty repo per invocation.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_dirty_canonical_sweep.models import (
    ModelDirtyCanonicalSweepCommand,
    ModelDirtyCanonicalSweepResult,
    ModelDirtyRepoShipResult,
)

logger = logging.getLogger(__name__)

_BRANCH_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


@runtime_checkable
class ProtocolGitRunner(Protocol):
    """Protocol for running git subprocess commands."""

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...


@runtime_checkable
class ProtocolGhRunner(Protocol):
    """Protocol for running gh CLI commands."""

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessGitRunner:
    """Production git runner using subprocess."""

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            text=True,
        )


class SubprocessGhRunner:
    """Production gh CLI runner using subprocess."""

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["gh", *args],
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            text=True,
        )


class HandlerDirtyCanonicalSweep:
    """Detect and auto-ship dirty canonical omni_home repo clones."""

    def __init__(
        self,
        git: ProtocolGitRunner | None = None,
        gh: ProtocolGhRunner | None = None,
    ) -> None:
        self._git: ProtocolGitRunner = git or SubprocessGitRunner()
        self._gh: ProtocolGhRunner = gh or SubprocessGhRunner()

    def handle(
        self,
        command: ModelDirtyCanonicalSweepCommand,
    ) -> ModelDirtyCanonicalSweepResult:
        omni_home = _resolve_omni_home(command.omni_home)
        worktrees_root = _resolve_worktrees_root(command.worktrees_root, omni_home)
        repos = command.repos or _discover_repos(omni_home)

        ship_results: list[ModelDirtyRepoShipResult] = []
        for repo in repos:
            repo_path = omni_home / repo
            if not (repo_path / ".git").exists():
                continue
            dirty_files = self._dirty_files(repo_path)
            if not dirty_files:
                continue

            if command.dry_run:
                ship_results.append(
                    ModelDirtyRepoShipResult(
                        repo=repo,
                        dirty_files=dirty_files,
                        status="dry_run",
                    )
                )
                continue

            result = self._ship_repo(
                repo=repo,
                repo_path=repo_path,
                dirty_files=dirty_files,
                worktrees_root=worktrees_root,
                pr_label=command.pr_label,
                base_branch=command.base_branch,
            )
            ship_results.append(result)

        shipped = sum(1 for r in ship_results if r.status == "shipped")
        failed = sum(1 for r in ship_results if r.status == "failed")
        return ModelDirtyCanonicalSweepResult(
            repos_checked=len(repos),
            repos_dirty=len(ship_results),
            repos_shipped=shipped,
            repos_failed=failed,
            dry_run=command.dry_run,
            results=ship_results,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dirty_files(self, repo_path: Path) -> list[str]:
        result = self._git.run(
            ["status", "--porcelain"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        lines = [line.rstrip() for line in result.stdout.splitlines() if line.strip()]
        # Porcelain v1: "XY filename" where XY is exactly 2 chars + space separator
        return [line[3:] for line in lines if len(line) > 3]

    def _canonical_head(self, repo_path: Path) -> str:
        """Return the canonical clone's current HEAD commit SHA.

        The dirty changes are relative to this commit, so the rescue worktree
        must branch from it (OMN-12636).
        """
        result = self._git.run(
            ["rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def _ship_repo(
        self,
        *,
        repo: str,
        repo_path: Path,
        dirty_files: list[str],
        worktrees_root: Path,
        pr_label: str,
        base_branch: str,
    ) -> ModelDirtyRepoShipResult:
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
        branch = f"auto-ship/{timestamp}/{repo}"
        worktree_path = worktrees_root / f"auto-ship-{timestamp}" / repo

        try:
            # Step 0: resolve the canonical clone's actual HEAD. The dirty
            # changes are relative to THIS commit, not origin/<base>. Basing the
            # rescue worktree on origin/<base> when the canonical clone is behind
            # that branch conflates "uncommitted local work" with "clone is
            # behind base", which produces a regressive partial PR that reverts
            # already-merged work (OMN-12636). Branch from the canonical HEAD so
            # the diff captures only the uncommitted local changes.
            head_sha = self._canonical_head(repo_path)

            # Step 1: create worktree from the canonical clone's HEAD.
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            self._git.run(
                [
                    "worktree",
                    "add",
                    str(worktree_path),
                    "-b",
                    branch,
                    head_sha,
                ],
                cwd=repo_path,
            )

            # Step 2: copy dirty files from canonical to worktree. A dirty entry
            # may be an untracked directory (e.g. ``evidence/OMN-12584/``);
            # read_bytes() on a directory raises "[Errno 21] Is a directory" and
            # the ship fails (OMN-12635). Expand directories to their
            # constituent files so every dirty path is copied byte-for-byte.
            for rel_path in dirty_files:
                src = repo_path / rel_path
                if not src.exists():
                    continue
                if src.is_dir():
                    for file_path in sorted(src.rglob("*")):
                        if not file_path.is_file():
                            continue
                        dst = worktree_path / file_path.relative_to(repo_path)
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(file_path.read_bytes())
                else:
                    dst = worktree_path / rel_path
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(src.read_bytes())

            # Step 3: commit in worktree
            self._git.run(["add", "-A"], cwd=worktree_path)
            commit_msg = (
                f"auto-ship(OMN-7466): rescue dirty canonical {repo} [{timestamp}]\n\n"
                f"Dirty files detected in canonical clone and auto-shipped:\n"
                + "\n".join(f"  {f}" for f in dirty_files)
            )
            self._git.run(
                ["commit", "-m", commit_msg],
                cwd=worktree_path,
            )

            # Step 4: push branch
            self._git.run(
                ["push", "-u", "origin", branch],
                cwd=worktree_path,
            )

            # Step 5: restore canonical to clean state (only after successful push)
            self._git.run(
                ["checkout", "--", "."],
                cwd=repo_path,
            )

            # Step 6: create PR
            pr_url = self._create_pr(
                repo=repo,
                branch=branch,
                dirty_files=dirty_files,
                worktree_path=worktree_path,
                pr_label=pr_label,
                base_branch=base_branch,
            )

            return ModelDirtyRepoShipResult(
                repo=repo,
                dirty_files=dirty_files,
                status="shipped",
                worktree_path=str(worktree_path),
                branch=branch,
                pr_url=pr_url,
            )

        except Exception as exc:
            logger.error("Failed to ship dirty canonical %s: %s", repo, exc)
            return ModelDirtyRepoShipResult(
                repo=repo,
                dirty_files=dirty_files,
                status="failed",
                error=str(exc),
            )

    def _create_pr(
        self,
        *,
        repo: str,
        branch: str,
        dirty_files: list[str],
        worktree_path: Path,
        pr_label: str,
        base_branch: str,
    ) -> str:
        file_list = "\n".join(f"- `{f}`" for f in dirty_files)
        body = (
            "## Auto-shipped by node_dirty_canonical_sweep (OMN-7466)\n\n"
            "Dirty files were detected in the canonical omni_home clone and "
            "rescued to this worktree before the next `git pull` could discard them.\n\n"
            f"### Files\n{file_list}\n\n"
            "**Review and merge or close.** This PR was created automatically "
            "by the dirty-canonical sweep cron."
        )
        result = self._gh.run(
            [
                "pr",
                "create",
                "--title",
                f"auto-ship({repo}): rescue uncommitted changes [{branch.split('/')[-1]}]",
                "--body",
                body,
                "--label",
                pr_label,
                "--head",
                branch,
                "--base",
                base_branch,
            ],
            cwd=worktree_path,
            capture_output=True,
        )
        return result.stdout.strip()


def _resolve_omni_home(override: str | None) -> Path:
    if override:
        return Path(override)
    return Path(os.environ["OMNI_HOME"])


def _resolve_worktrees_root(override: str | None, omni_home: Path) -> Path:
    if override:
        return Path(override)
    raw = os.environ.get("OMNI_WORKTREES_ROOT")
    if raw:
        return Path(raw)
    return omni_home / "omni_worktrees"


def _discover_repos(omni_home: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in omni_home.iterdir()
        if entry.is_dir() and (entry / ".git").exists()
    )


__all__: list[str] = [
    "HandlerDirtyCanonicalSweep",
    "ProtocolGhRunner",
    "ProtocolGitRunner",
]
