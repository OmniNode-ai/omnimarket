# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane worktree retention: frees finished lane trees under ``~/lab-run/runs``.

The file retention entry ``lab-run-lane-dirs`` skips any unit holding a ``.git`` entry, so lane
worktrees and their ``.ctx`` directories were never freed on a lab host (h101, 2026-11-05: 349
worktrees, 139 GB, and 466 ``.ctx`` directories, 85 GB). This pass owns those units, with its own
fences, because the host's worktree triage does not cover ``~/lab-run``:

- process fence: a unit a live process names (command line, environment, working directory or an
  open file, from /proc or from lsof and ps on macOS) is kept; an unreadable process table keeps
  everything;
- activity fence: a unit with any entry modified inside the bound is kept (``idle_days`` for a
  worktree, ``ctx_keep_days`` for a ``.ctx`` directory, with a seven-day floor);
- git fence (worktrees): every repository in the unit must be clean, including untracked files,
  with every local commit on a remote ref; a clone's other refs and stashes count, and a clone
  that still owns a linked worktree's git directory is kept.

A ``.ctx`` directory is also kept while a live process names its run tree. A unit with no git
repository in it is not this pass's: file retention archives it. Symlinks are never followed or
removed. A removed unit is renamed away first, so no reader sees a half-removed tree, and each
canonical clone that held a removed linked worktree has its stale registration pruned.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import yaml

from omnimarket.handlers.host_process_references import (
    path_referenced,
    read_process_references,
)
from omnimarket.nodes.node_retention_cleanup.models.model_lane_worktree_retention import (
    ModelLaneWorktreePolicy,
    ModelLaneWorktreeRetentionRequest,
    ModelLaneWorktreeRetentionResult,
)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_REMOVING = re.compile(r"^\..+\.removing\.[0-9]+$")
_CTX = ".ctx"


def load_lane_worktree_policy(
    contract_path: Path = _CONTRACT_PATH,
) -> ModelLaneWorktreePolicy:
    raw = yaml.safe_load(contract_path.read_text()) or {}
    block = (raw.get("retention_policy") or {}).get("lane_worktree_retention")
    if not block:
        msg = f"contract.yaml at {contract_path} declares no retention_policy.lane_worktree_retention"
        raise RuntimeError(msg)
    return ModelLaneWorktreePolicy.model_validate(block)


def _git(cwd: Path, *args: str) -> str:
    # A lane engine may export GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE; read the candidate
    # repository, never that engine's checkout, and never take an index lock.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def _fail(error: OSError) -> None:
    raise error


def _repositories(unit: Path) -> list[Path]:
    """Every directory in the unit carrying a ``.git`` entry, nested ones included."""
    repos: list[Path] = []
    for directory, dirs, files in os.walk(unit, onerror=_fail):
        if ".git" in dirs or ".git" in files:
            repos.append(Path(directory))
            if ".git" in dirs:
                dirs.remove(".git")
    return repos


def _active_since(unit: Path, cutoff: float) -> bool:
    """True when any entry outside the git object stores changed at or after ``cutoff``."""
    if unit.lstat().st_mtime >= cutoff:
        return True
    for directory, dirs, files in os.walk(unit, onerror=_fail):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in (*dirs, *files):
            if (Path(directory) / name).lstat().st_mtime >= cutoff:
                return True
    return False


def _size(unit: Path) -> int:
    total = 0
    for directory, _dirs, files in os.walk(unit):
        for name in files:
            try:
                total += (Path(directory) / name).lstat().st_size
            except OSError:
                continue
    return total


def _git_keep_reason(repo: Path) -> str:
    """Empty when the repository is clean and every local commit is on a remote ref."""
    dot = repo / ".git"
    owns_store = dot.is_dir() and not dot.is_symlink()
    try:
        if owns_store:
            linked = dot / "worktrees"
            if linked.is_symlink() or (linked.is_dir() and any(linked.iterdir())):
                return "holds-worktree-git-dir"
        if Path(_git(repo, "rev-parse", "--show-toplevel")).resolve() != repo.resolve():
            return "worktree-path-mismatch"
        if _git(
            repo,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ):
            return "uncommitted-changes"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "status-unreadable"
    try:
        # A clone's object store goes with it: count its other branches and its stash.
        # A linked worktree's store stays in the canonical clone: only its HEAD is at risk.
        refs = ["--all"] if owns_store else []
        if (
            _git(repo, "rev-list", "--count", "HEAD", *refs, "--not", "--remotes")
            != "0"
        ):
            return "unpushed-commits"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "remotes-unreadable"
    return ""


def _common_dir(repo: Path) -> Path | None:
    if not (repo / ".git").is_file():
        return None
    try:
        return Path(
            _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        )
    except (OSError, subprocess.SubprocessError):
        return None


class HandlerLaneWorktreeRetention:
    """Remove finished lane worktrees and stale ``.ctx`` directories under one runs dir."""

    def __init__(
        self, process_references: Callable[[], list[str] | None] | None = None
    ) -> None:
        self.process_references = process_references or read_process_references

    def handle(
        self, request: ModelLaneWorktreeRetentionRequest
    ) -> ModelLaneWorktreeRetentionResult:
        runs = request.runs_dir.expanduser()
        if runs.is_symlink() or not runs.is_dir():
            return ModelLaneWorktreeRetentionResult(status="absent")
        references = self.process_references()
        if references is None:
            return ModelLaneWorktreeRetentionResult(
                status="skipped",
                reason="process table unavailable; live lane trees cannot be checked",
            )
        idle_cutoff = (request.now - dt.timedelta(days=request.idle_days)).timestamp()
        ctx_cutoff = (
            request.now - dt.timedelta(days=request.ctx_keep_days)
        ).timestamp()
        removed: list[str] = []
        removed_ctx: list[str] = []
        kept: dict[str, str] = {}
        errors: list[str] = []
        freed = 0
        for unit in sorted(runs.iterdir()):
            name = unit.name
            if unit.is_symlink() or not unit.is_dir():
                continue
            if _REMOVING.fullmatch(name):
                if not request.dry_run:
                    try:
                        shutil.rmtree(unit)
                    except OSError as exc:
                        errors.append(f"{name}: {exc}"[:300])
                continue
            try:
                if name.endswith(_CTX) and len(name) > len(_CTX):
                    reason = self._ctx_keep_reason(
                        unit, runs / name[: -len(_CTX)], references, ctx_cutoff
                    )
                    repos: list[Path] = []
                else:
                    repos = _repositories(unit)
                    if not repos:
                        continue  # not a worktree: file retention owns it
                    reason = self._worktree_keep_reason(
                        unit, repos, references, idle_cutoff
                    )
            except OSError as exc:
                kept[name] = f"tree-unreadable: {exc}"[:300]
                continue
            if reason:
                kept[name] = reason
                continue
            size = _size(unit)
            if not request.dry_run:
                try:
                    self._remove(runs, unit, repos)
                except OSError as exc:
                    errors.append(f"{name}: {exc}"[:300])
                    continue
            freed += size
            (removed_ctx if not repos else removed).append(name)
        return ModelLaneWorktreeRetentionResult(
            status="dry-run" if request.dry_run else "ran",
            removed_worktrees=tuple(removed),
            removed_ctx=tuple(removed_ctx),
            kept=kept,
            freed_bytes=freed,
            errors=tuple(errors),
        )

    @staticmethod
    def _ctx_keep_reason(
        ctx: Path, run_tree: Path, references: list[str], cutoff: float
    ) -> str:
        if path_referenced(ctx, references) or path_referenced(run_tree, references):
            return "in-use"
        if _active_since(ctx, cutoff):
            return "active"
        return ""

    @staticmethod
    def _worktree_keep_reason(
        unit: Path, repos: list[Path], references: list[str], cutoff: float
    ) -> str:
        if path_referenced(unit, references):
            return "in-use"
        if _active_since(unit, cutoff):
            return "active"
        for repo in repos:
            reason = _git_keep_reason(repo)
            if reason:
                return f"{reason}: {repo.relative_to(unit.parent)}"
        return ""

    @staticmethod
    def _remove(runs: Path, unit: Path, repos: list[Path]) -> None:
        commons = {common for repo in repos if (common := _common_dir(repo))}
        staged = runs / f".{unit.name}.removing.{os.getpid()}"
        os.rename(unit, staged)
        shutil.rmtree(staged)
        for common in sorted(commons):
            # The canonical clone still lists the removed tree until it is pruned.
            try:
                _git(common, "worktree", "prune")
            except (OSError, subprocess.SubprocessError):
                continue
