# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_dirty_canonical_sweep (OMN-13683, WS-5 Wave 9).

Variant A (EFFECT): drives the real ``HandlerDirtyCanonicalSweep.handle`` over a
matrix of canonical-clone states. The git/gh I/O boundary is satisfied by injected
mock runners implementing ``ProtocolGitRunner`` / ``ProtocolGhRunner`` (the _Mock*
constructor pattern) — we NEVER monkeypatch subprocess and never run real git/gh.
The omni_home tree is a synthetic ``tmp_path`` fixture; no canonical clone is
touched. Read-only / in-memory in the sense required by this wave: no live repo,
network, or .201.

Asserts typed result counts (``repos_checked/dirty/shipped/failed``) and per-repo
``ModelDirtyRepoShipResult`` fields (``status``, ``branch``, ``pr_url``, ``error``,
``dirty_files``). Negative control: a push failure must surface as a ``failed``
ship result with a populated ``error``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_dirty_canonical_sweep.handlers.handler_dirty_canonical_sweep import (
    HandlerDirtyCanonicalSweep,
)
from omnimarket.nodes.node_dirty_canonical_sweep.models import (
    ModelDirtyCanonicalSweepCommand,
)


class _MockGitRunner:
    """In-memory git runner: porcelain status per repo, no real git calls."""

    def __init__(
        self,
        porcelain: dict[str, list[str]],
        fail_push_repos: set[str] | None = None,
    ) -> None:
        self._porcelain = porcelain
        self._fail_push = fail_push_repos or set()
        self.calls: list[tuple[str, str]] = []  # (subcommand, cwd.name)

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        sub = args[0]
        self.calls.append((sub, cwd.name))
        if sub == "status":
            dirty = self._porcelain.get(cwd.name, [])
            stdout = "".join(f" M {f}\n" for f in dirty)
            return subprocess.CompletedProcess(["git", *args], 0, stdout, "")
        if sub == "rev-parse":
            return subprocess.CompletedProcess(["git", *args], 0, "deadbeef0000\n", "")
        if sub == "push":
            # cwd here is the worktree dir whose name is the repo.
            if cwd.name in self._fail_push:
                raise subprocess.CalledProcessError(1, ["git", "push"], "", "boom")
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        return subprocess.CompletedProcess(["git", *args], 0, "", "")


class _MockGhRunner:
    """In-memory gh runner: pr create returns a deterministic URL."""

    def __init__(self) -> None:
        self.created: list[str] = []

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["pr", "create"]:
            url = f"https://github.com/OmniNode-ai/{cwd.name}/pull/999"
            self.created.append(url)
            return subprocess.CompletedProcess(["gh", *args], 0, url + "\n", "")
        return subprocess.CompletedProcess(["gh", *args], 0, "", "")


def _make_repo(
    omni_home: Path, name: str, dirty_files: list[str], *, has_git: bool = True
) -> None:
    repo = omni_home / name
    repo.mkdir(parents=True, exist_ok=True)
    if has_git:
        (repo / ".git").mkdir()
    for rel in dirty_files:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"dirty content for {rel}\n")


# Each case returns (command, mock_git, mock_gh) and expected counts/fields.
def _case_all_clean(
    tmp: Path,
) -> tuple[
    ModelDirtyCanonicalSweepCommand, _MockGitRunner, _MockGhRunner, dict[str, object]
]:
    home = tmp / "omni_home"
    _make_repo(home, "repo_a", [])
    _make_repo(home, "repo_b", [])
    git = _MockGitRunner(porcelain={})
    gh = _MockGhRunner()
    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(home), worktrees_root=str(tmp / "wt"), repos=["repo_a", "repo_b"]
    )
    return (
        cmd,
        git,
        gh,
        {
            "repos_checked": 2,
            "repos_dirty": 0,
            "repos_shipped": 0,
            "repos_failed": 0,
        },
    )


def _case_one_dirty_dry_run(
    tmp: Path,
) -> tuple[
    ModelDirtyCanonicalSweepCommand, _MockGitRunner, _MockGhRunner, dict[str, object]
]:
    home = tmp / "omni_home"
    _make_repo(home, "repo_a", ["a.py"])
    git = _MockGitRunner(porcelain={"repo_a": ["a.py"]})
    gh = _MockGhRunner()
    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(home),
        worktrees_root=str(tmp / "wt"),
        repos=["repo_a"],
        dry_run=True,
    )
    return (
        cmd,
        git,
        gh,
        {
            "repos_checked": 1,
            "repos_dirty": 1,
            "repos_shipped": 0,
            "repos_failed": 0,
            "result_status": "dry_run",
            "dry_run": True,
        },
    )


def _case_one_dirty_ship(
    tmp: Path,
) -> tuple[
    ModelDirtyCanonicalSweepCommand, _MockGitRunner, _MockGhRunner, dict[str, object]
]:
    home = tmp / "omni_home"
    _make_repo(home, "repo_a", ["a.py", "docs/b.md"])
    git = _MockGitRunner(porcelain={"repo_a": ["a.py", "docs/b.md"]})
    gh = _MockGhRunner()
    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(home), worktrees_root=str(tmp / "wt"), repos=["repo_a"]
    )
    return (
        cmd,
        git,
        gh,
        {
            "repos_checked": 1,
            "repos_dirty": 1,
            "repos_shipped": 1,
            "repos_failed": 0,
            "result_status": "shipped",
            "pr_created": True,
        },
    )


def _case_multi_dirty_ship(
    tmp: Path,
) -> tuple[
    ModelDirtyCanonicalSweepCommand, _MockGitRunner, _MockGhRunner, dict[str, object]
]:
    home = tmp / "omni_home"
    _make_repo(home, "repo_a", ["a.py"])
    _make_repo(home, "repo_b", ["b.py"])
    git = _MockGitRunner(porcelain={"repo_a": ["a.py"], "repo_b": ["b.py"]})
    gh = _MockGhRunner()
    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(home), worktrees_root=str(tmp / "wt"), repos=["repo_a", "repo_b"]
    )
    return (
        cmd,
        git,
        gh,
        {
            "repos_checked": 2,
            "repos_dirty": 2,
            "repos_shipped": 2,
            "repos_failed": 0,
        },
    )


def _case_push_failure(
    tmp: Path,
) -> tuple[
    ModelDirtyCanonicalSweepCommand, _MockGitRunner, _MockGhRunner, dict[str, object]
]:
    home = tmp / "omni_home"
    _make_repo(home, "repo_a", ["a.py"])
    git = _MockGitRunner(porcelain={"repo_a": ["a.py"]}, fail_push_repos={"repo_a"})
    gh = _MockGhRunner()
    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(home), worktrees_root=str(tmp / "wt"), repos=["repo_a"]
    )
    return (
        cmd,
        git,
        gh,
        {
            "repos_checked": 1,
            "repos_dirty": 1,
            "repos_shipped": 0,
            "repos_failed": 1,
            "result_status": "failed",
            "has_error": True,
        },
    )


def _case_skip_non_git(
    tmp: Path,
) -> tuple[
    ModelDirtyCanonicalSweepCommand, _MockGitRunner, _MockGhRunner, dict[str, object]
]:
    home = tmp / "omni_home"
    _make_repo(home, "repo_a", ["a.py"])
    _make_repo(home, "not_a_repo", ["x.py"], has_git=False)
    git = _MockGitRunner(porcelain={"repo_a": ["a.py"], "not_a_repo": ["x.py"]})
    gh = _MockGhRunner()
    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(home),
        worktrees_root=str(tmp / "wt"),
        repos=["repo_a", "not_a_repo"],
    )
    return (
        cmd,
        git,
        gh,
        {
            "repos_checked": 2,
            "repos_dirty": 1,  # not_a_repo skipped (no .git)
            "repos_shipped": 1,
            "repos_failed": 0,
        },
    )


_CASES: list[
    tuple[
        str,
        Callable[
            [Path],
            tuple[
                ModelDirtyCanonicalSweepCommand,
                _MockGitRunner,
                _MockGhRunner,
                dict[str, object],
            ],
        ],
    ]
] = [
    ("all-clean-no-ship", _case_all_clean),
    ("one-dirty-dry-run", _case_one_dirty_dry_run),
    ("one-dirty-ship", _case_one_dirty_ship),
    ("multi-dirty-ship", _case_multi_dirty_ship),
    ("push-failure-negative-control", _case_push_failure),
    ("skip-non-git-clone", _case_skip_non_git),
]


@pytest.mark.integration
@pytest.mark.parametrize("builder", [c[1] for c in _CASES], ids=[c[0] for c in _CASES])
def test_dirty_canonical_sweep_multiparam(
    tmp_path: Path,
    builder: Callable[
        [Path],
        tuple[
            ModelDirtyCanonicalSweepCommand,
            _MockGitRunner,
            _MockGhRunner,
            dict[str, object],
        ],
    ],
) -> None:
    cmd, git, gh, exp = builder(tmp_path)
    handler = HandlerDirtyCanonicalSweep(git=git, gh=gh)

    result = handler.handle(cmd)

    assert result.repos_checked == exp["repos_checked"]
    assert result.repos_dirty == exp["repos_dirty"]
    assert result.repos_shipped == exp["repos_shipped"]
    assert result.repos_failed == exp["repos_failed"]
    # result count integrity: dirty == number of per-repo ship results.
    assert len(result.results) == exp["repos_dirty"]

    if "dry_run" in exp:
        assert result.dry_run is exp["dry_run"]
    if "result_status" in exp:
        assert result.results[0].status == exp["result_status"]
    if exp.get("pr_created"):
        assert result.results[0].pr_url.endswith("/pull/999")
        assert result.results[0].branch.startswith("auto-ship/")
        assert result.results[0].dirty_files  # non-empty
        assert gh.created  # gh pr create was actually invoked
    if exp.get("has_error"):
        assert result.results[0].error  # populated failure detail
        assert not gh.created  # PR never created on a failed ship


def test_negative_control_push_failure_does_not_ship(tmp_path: Path) -> None:
    """A git push failure MUST yield a failed ship result, never a silent success."""
    cmd, git, gh, _ = _case_push_failure(tmp_path)
    result = HandlerDirtyCanonicalSweep(git=git, gh=gh).handle(cmd)
    assert result.repos_failed == 1
    assert result.repos_shipped == 0
    assert result.results[0].status == "failed"
    assert "boom" in result.results[0].error or result.results[0].error
