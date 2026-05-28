# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDirtyCanonicalSweep."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_dirty_canonical_sweep.handlers import (
    HandlerDirtyCanonicalSweep,
)
from omnimarket.nodes.node_dirty_canonical_sweep.models import (
    ModelDirtyCanonicalSweepCommand,
)


class _FakeGitRunner:
    """Records calls; returns configurable outputs per (args[0], cwd.name) key."""

    def __init__(self, outputs: dict[tuple[str, str], str] | None = None) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self._outputs = outputs or {}

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, cwd))
        key = (args[0], cwd.name)
        stdout = self._outputs.get(key, "")
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=0, stdout=stdout
        )


class _FakeGhRunner:
    """Records gh calls; always returns a dummy PR URL."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, cwd))
        return subprocess.CompletedProcess(
            args=["gh", *args],
            returncode=0,
            stdout="https://github.com/OmniNode-ai/myrepo/pull/999\n",
        )


@pytest.mark.unit
def test_dry_run_reports_dirty_repos_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omni_home = tmp_path / "omni_home"
    repo_dir = omni_home / "myrepo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    git = _FakeGitRunner(outputs={("status", "myrepo"): " M src/foo.py\n?? bar.py\n"})
    gh = _FakeGhRunner()
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(omni_home),
        worktrees_root=str(tmp_path / "worktrees"),
        repos=["myrepo"],
        dry_run=True,
    )
    result = HandlerDirtyCanonicalSweep(git=git, gh=gh).handle(cmd)

    assert result.dry_run is True
    assert result.repos_checked == 1
    assert result.repos_dirty == 1
    assert result.repos_shipped == 0
    assert result.repos_failed == 0
    assert len(result.results) == 1
    assert result.results[0].status == "dry_run"
    assert result.results[0].repo == "myrepo"
    assert "src/foo.py" in result.results[0].dirty_files
    assert "bar.py" in result.results[0].dirty_files
    # No git write commands should have been called
    write_subcommands = {call[0][0] for call in git.calls if call[0][0] != "status"}
    assert write_subcommands == set(), (
        f"Unexpected git write calls: {write_subcommands}"
    )
    assert gh.calls == []


@pytest.mark.unit
def test_clean_repo_produces_no_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omni_home = tmp_path / "omni_home"
    repo_dir = omni_home / "cleanrepo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    git = _FakeGitRunner(outputs={("status", "cleanrepo"): ""})
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(omni_home),
        worktrees_root=str(tmp_path / "worktrees"),
        repos=["cleanrepo"],
    )
    result = HandlerDirtyCanonicalSweep(git=git).handle(cmd)

    assert result.repos_checked == 1
    assert result.repos_dirty == 0
    assert result.repos_shipped == 0
    assert result.results == []


@pytest.mark.unit
def test_ship_calls_git_in_correct_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omni_home = tmp_path / "omni_home"
    repo_dir = omni_home / "myrepo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    dirty_file = repo_dir / "src" / "changed.py"
    dirty_file.parent.mkdir(parents=True)
    dirty_file.write_text("# changed\n", encoding="utf-8")

    git = _FakeGitRunner(outputs={("status", "myrepo"): " M src/changed.py\n"})
    gh = _FakeGhRunner()
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(omni_home),
        worktrees_root=str(tmp_path / "worktrees"),
        repos=["myrepo"],
        dry_run=False,
    )
    result = HandlerDirtyCanonicalSweep(git=git, gh=gh).handle(cmd)

    assert result.repos_shipped == 1
    assert result.repos_failed == 0
    shipped = result.results[0]
    assert shipped.status == "shipped"
    assert shipped.pr_url == "https://github.com/OmniNode-ai/myrepo/pull/999"

    git_subcommands = [call[0][0] for call in git.calls]
    # Must include: status, worktree add, add -A, commit, push, checkout --
    assert "status" in git_subcommands
    assert "worktree" in git_subcommands
    assert "add" in git_subcommands
    assert "commit" in git_subcommands
    assert "push" in git_subcommands
    assert "checkout" in git_subcommands

    # checkout -- . on canonical must come AFTER push (restore only on success)
    push_idx = next(i for i, c in enumerate(git.calls) if c[0][0] == "push")
    checkout_canonical_calls = [
        i for i, c in enumerate(git.calls) if c[0][0] == "checkout" and c[1] == repo_dir
    ]
    assert checkout_canonical_calls, "checkout -- . on canonical not called"
    assert all(idx > push_idx for idx in checkout_canonical_calls)
    worktree_call = next(call for call in git.calls if call[0][0] == "worktree")
    assert "origin/dev" in worktree_call[0]

    # gh pr create must have been called
    assert any("pr" in call[0] and "create" in call[0] for call in gh.calls)
    pr_call = next(call for call in gh.calls if "pr" in call[0] and "create" in call[0])
    assert "--base" in pr_call[0]
    assert pr_call[0][pr_call[0].index("--base") + 1] == "dev"


@pytest.mark.unit
def test_handler_skips_dirs_without_dot_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omni_home = tmp_path / "omni_home"
    (omni_home / "notarepo").mkdir(parents=True)  # no .git
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    git = _FakeGitRunner()
    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(omni_home),
        repos=["notarepo"],
    )
    result = HandlerDirtyCanonicalSweep(git=git).handle(cmd)

    assert result.repos_checked == 1
    assert result.repos_dirty == 0
    assert git.calls == []
