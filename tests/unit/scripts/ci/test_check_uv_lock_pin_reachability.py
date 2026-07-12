# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14450: uv.lock git-SHA pins must be reachable from a remote branch.

The load-bearing test is `test_naive_unpruned_check_gives_a_false_pass`: it pins
the reason this gate must prune. Without the prune the check is green on a lock
that is already broken for CI and for every fresh clone (OMN-14447).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.ci import check_uv_lock_pin_reachability
from scripts.ci.check_uv_lock_pin_reachability import extract_pins, is_reachable, main

_LOCK = """
[[package]]
name = "omnibase-core"
version = "0.46.8"
source = {{ git = "https://github.com/OmniNode-ai/omnibase_core.git?rev={rev}#{rev}" }}
"""


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, str, str]:
    """An origin + a clone of it. Returns (clones_root, kept_sha, deleted_branch_sha).

    Mirrors OMN-14447 exactly: a commit on a feature branch is fetched by the clone,
    then the branch is deleted upstream (as GitHub does on merge).
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q", "-b", "dev"], cwd=origin)
    _git(["config", "user.email", "t@t"], cwd=origin)
    _git(["config", "user.name", "t"], cwd=origin)
    (origin / "f").write_text("1")
    _git(["add", "-A"], cwd=origin)
    _git(["commit", "-qm", "on dev"], cwd=origin)
    kept = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=origin, capture_output=True, text=True
    ).stdout.strip()

    _git(["checkout", "-q", "-b", "feature"], cwd=origin)
    (origin / "f").write_text("2")
    _git(["commit", "-qam", "on feature"], cwd=origin)
    doomed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=origin, capture_output=True, text=True
    ).stdout.strip()
    _git(["checkout", "-q", "dev"], cwd=origin)

    root = tmp_path / "clones"
    root.mkdir()
    _git(["clone", "-q", str(origin), "omnibase_core"], cwd=root)

    # The branch merges and GitHub deletes it. The clone keeps its stale ref.
    _git(["branch", "-q", "-D", "feature"], cwd=origin)
    return root, kept, doomed


def test_extract_pins_reads_git_sha_pins() -> None:
    assert extract_pins(_LOCK.format(rev="a" * 40)) == [("omnibase_core", "a" * 40)]


def test_reachable_commit_on_dev_passes(clone: tuple[Path, str, str]) -> None:
    root, kept, _ = clone
    assert is_reachable(root / "omnibase_core", kept) is True


def test_deleted_branch_head_is_unreachable(clone: tuple[Path, str, str]) -> None:
    """OMN-14447: a feature-branch head, after the branch is deleted upstream."""
    root, _, doomed = clone
    assert is_reachable(root / "omnibase_core", doomed) is False


def test_naive_unpruned_check_gives_a_false_pass(clone: tuple[Path, str, str]) -> None:
    """THE TRAP -- why this gate must prune, and why it only means anything in CI.

    Same clone, same commit, same deleted branch. Without the prune the stale
    remote-tracking ref survives and the commit reports as reachable: a green
    check on a lock that is already broken for CI and every fresh clone.
    """
    root, _, doomed = clone
    repo = root / "omnibase_core"
    assert is_reachable(repo, doomed, prune=False) is True  # <-- the false PASS
    assert is_reachable(repo, doomed, prune=True) is False  # <-- the truth


def test_fetch_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "omnibase_core"
    (repo / ".git").mkdir(parents=True)

    def fake_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert cwd == repo
        if args == ["fetch", "origin", "--prune", "--quiet"]:
            return subprocess.CompletedProcess(["git", *args], 1, "", "network down")
        raise AssertionError(f"unexpected git call after failed fetch: {args}")

    monkeypatch.setattr(check_uv_lock_pin_reachability, "_git", fake_git)

    assert is_reachable(repo, "a" * 40) is False


def test_missing_clone_fails_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(_LOCK.format(rev="a" * 40), encoding="utf-8")
    clones = tmp_path / "clones"
    clones.mkdir()

    assert main(["--lock", str(lock), "--clones-root", str(clones)]) == 1

    out = capsys.readouterr().out
    assert "UNVERIFIED  omnibase_core @ aaaaaaaaa" in out
    assert "could not be verified" in out
