# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDirtyCanonicalSweep."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_dirty_canonical_sweep.handlers import (
    HandlerDirtyCanonicalSweep,
)
from omnimarket.nodes.node_dirty_canonical_sweep.models import (
    ModelDirtyCanonicalSweepCommand,
)

# Mirrors the accepted forms enforced by the two required CI gates the
# auto-ship rescue PR must satisfy (OMN-12638):
#
#   - pr-title / check-title
#     (onex_change_control/.github/workflows/pr-title-check-reusable.yml):
#     a title passes when it contains an OMN-XXXX reference (or an exempt
#     deps/release prefix, which auto-ship is not).
#   - verify / Run Receipt-Gate
#     (omnibase_core validator_receipt_gate): the body must cite an OMN-XXXX
#     ticket. A bare OMN-XXXX mention in the title is used as a fallback ticket
#     source; free-text receipt-gate bypass tokens are rejected by the OMN-10417
#     hardening, so this node satisfies the gate via the ticket-citation path
#     against epic OMN-7466 (which carries a contract + PASS dod_evidence
#     receipts in onex_change_control).
_PR_TITLE_TICKET_RE = re.compile(r"OMN-[0-9]+")
_RECEIPT_GATE_TICKET_RE = re.compile(r"\bOMN-(\d+)\b", re.IGNORECASE)


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

    head_sha = "1111111122222222333333334444444455555555"
    git = _FakeGitRunner(
        outputs={
            ("status", "myrepo"): " M src/changed.py\n",
            ("rev-parse", "myrepo"): f"{head_sha}\n",
        }
    )
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
    # Must include: status, rev-parse, worktree add, add -A, commit, push,
    # checkout --
    assert "status" in git_subcommands
    assert "rev-parse" in git_subcommands
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
    # Worktree is based on the canonical HEAD SHA (the commit the dirty changes
    # are relative to), NOT origin/dev (which can be ahead and cause a
    # regressive partial PR).
    worktree_call = next(call for call in git.calls if call[0][0] == "worktree")
    assert head_sha in worktree_call[0]
    assert "origin/dev" not in worktree_call[0]

    # gh pr create must have been called
    assert any("pr" in call[0] and "create" in call[0] for call in gh.calls)
    pr_call = next(call for call in gh.calls if "pr" in call[0] and "create" in call[0])
    assert "--base" in pr_call[0]
    assert pr_call[0][pr_call[0].index("--base") + 1] == "dev"


@pytest.mark.unit
def test_pr_title_and_body_satisfy_required_ci_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-ship PRs must be mergeable by design (OMN-12638).

    Regression for omniclaude PR #1722, which was titled
    ``auto-ship(omniclaude): rescue uncommitted changes`` and cited no ticket in
    its body. That PR failed both ``pr-title / check-title`` and
    ``verify / Run Receipt-Gate`` and could never merge. The generated title
    must carry an OMN-XXXX reference, and the body must cite an OMN-XXXX ticket
    so the receipt gate accepts it via the ticket-citation path.
    """
    omni_home = tmp_path / "omni_home"
    repo_dir = omni_home / "myrepo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    dirty_file = repo_dir / "uv.lock"
    dirty_file.write_text("# changed\n", encoding="utf-8")

    git = _FakeGitRunner(outputs={("status", "myrepo"): " M uv.lock\n"})
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

    pr_call = next(call for call in gh.calls if "pr" in call[0] and "create" in call[0])
    args = pr_call[0]
    title = args[args.index("--title") + 1]
    body = args[args.index("--body") + 1]

    # pr-title / check-title: the title must contain an OMN-XXXX reference
    # (auto-ship is not an exempt deps/release prefix).
    assert _PR_TITLE_TICKET_RE.search(title), (
        f"PR title must contain OMN-XXXX to pass pr-title gate; got {title!r}"
    )
    # The cited ticket is the tracking epic OMN-7466.
    assert "OMN-7466" in title

    # verify / Run Receipt-Gate: the body must cite an OMN-XXXX ticket so the
    # gate can resolve dod_evidence. Free-text receipt-gate bypass tokens are
    # rejected by the OMN-10417 hardening, so a real ticket citation is used.
    assert _RECEIPT_GATE_TICKET_RE.search(body), (
        f"PR body must cite OMN-XXXX to pass the receipt gate; got {body!r}"
    )
    assert "OMN-7466" in body
    # Must not emit a free-text receipt-gate bypass token: the validator
    # hard-fails unapproved skip tokens, so the node must rely on ticket
    # citation, not a skip line.
    forbidden_token = "[" "skip-" "receipt-gate:"
    assert forbidden_token not in body


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


@pytest.mark.unit
def test_worktree_based_on_canonical_head_not_origin_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-12636: rescue worktree must branch from the canonical clone's actual
    HEAD (the commit the dirty changes are relative to), NOT origin/<base>.

    Basing on origin/dev when the canonical clone is behind dev conflates
    "uncommitted local work" with "clone is behind dev" and produces a
    regressive partial PR that reverts already-merged dev work.
    """
    omni_home = tmp_path / "omni_home"
    repo_dir = omni_home / "myrepo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    dirty_file = repo_dir / "src" / "changed.py"
    dirty_file.parent.mkdir(parents=True)
    dirty_file.write_text("# changed\n", encoding="utf-8")

    head_sha = "abc123def4567890abc123def4567890abc123de"
    git = _FakeGitRunner(
        outputs={
            ("status", "myrepo"): " M src/changed.py\n",
            ("rev-parse", "myrepo"): f"{head_sha}\n",
        }
    )
    gh = _FakeGhRunner()
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(omni_home),
        worktrees_root=str(tmp_path / "worktrees"),
        repos=["myrepo"],
        dry_run=False,
    )
    result = HandlerDirtyCanonicalSweep(git=git, gh=gh).handle(cmd)

    assert result.repos_shipped == 1, result.results[0].error
    worktree_call = next(call for call in git.calls if call[0][0] == "worktree")
    # The worktree must be created from the canonical HEAD SHA, never origin/dev.
    assert head_sha in worktree_call[0], (
        f"worktree must branch from canonical HEAD {head_sha}, got {worktree_call[0]}"
    )
    assert "origin/dev" not in worktree_call[0], (
        "worktree must NOT branch from origin/dev (stale-base regression)"
    )


@pytest.mark.unit
def test_dirty_untracked_directory_is_expanded_to_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-12635: a dirty entry may be an untracked DIRECTORY (e.g.
    ``evidence/OMN-12584/``). read_bytes() on a directory raises
    ``[Errno 21] Is a directory`` and the ship fails. The copy loop must
    recurse into directories and copy each constituent file.
    """
    omni_home = tmp_path / "omni_home"
    repo_dir = omni_home / "myrepo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    # Untracked directory with nested files (git porcelain reports the dir).
    evidence_dir = repo_dir / "evidence" / "OMN-12584"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "receipt.json").write_text('{"ok": true}\n', encoding="utf-8")
    nested = evidence_dir / "logs"
    nested.mkdir()
    (nested / "run.log").write_text("line1\nline2\n", encoding="utf-8")

    git = _FakeGitRunner(
        outputs={
            ("status", "myrepo"): "?? evidence/OMN-12584/\n",
            ("rev-parse", "myrepo"): "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n",
        }
    )
    gh = _FakeGhRunner()
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    cmd = ModelDirtyCanonicalSweepCommand(
        omni_home=str(omni_home),
        worktrees_root=str(tmp_path / "worktrees"),
        repos=["myrepo"],
        dry_run=False,
    )
    result = HandlerDirtyCanonicalSweep(git=git, gh=gh).handle(cmd)

    assert result.repos_shipped == 1, result.results[0].error
    assert result.repos_failed == 0
    # The worktree is created from the canonical HEAD; the _FakeGitRunner does
    # not materialize files, so verify no "Is a directory" error was raised
    # (the bug surfaced as a failed ship with that errno message).
    assert "Is a directory" not in result.results[0].error
