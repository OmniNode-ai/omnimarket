# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verify adapter arguments and failure parsing without running external tools."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnimarket.nodes.node_pr_delegated_fix_effect.handlers import (
    handler_delegated_fix as module,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.handler_delegated_fix import (
    GitDiffAdapter,
    GitWorktreeResolver,
    LivePrPolishRunner,
    PrPolishRunOutcome,
    RuffFixRunner,
    _parse_shortstat_lines,
    _resolve_pr_head_branch,
    _run_checked,
)

pytestmark = pytest.mark.unit


def test_explicit_worktree_must_exist(tmp_path: Path) -> None:
    resolver = GitWorktreeResolver()
    assert (
        resolver.resolve(
            repo="owner/repo", pr_number=1, ticket_id=None, explicit_path=str(tmp_path)
        )
        == tmp_path
    )
    with pytest.raises(RuntimeError, match="does not exist"):
        resolver.resolve(
            repo="owner/repo",
            pr_number=1,
            ticket_id=None,
            explicit_path=str(tmp_path / "absent"),
        )


@pytest.mark.parametrize("ticket", [None, "TEST-1"])
def test_existing_worktree_precedes_canonical_clone_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ticket: str | None,
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMNI_WORKTREES", str(tmp_path / "worktrees"))
    candidate = tmp_path / "worktrees" / (ticket or "pr-42") / "repo"
    candidate.mkdir(parents=True)
    run = Mock()
    monkeypatch.setattr(module, "_run_checked", run)
    assert (
        GitWorktreeResolver().resolve(
            repo="owner/repo", pr_number=42, ticket_id=ticket, explicit_path=None
        )
        == candidate
    )
    run.assert_not_called()


def test_missing_canonical_clone_refuses_auto_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("OMNI_WORKTREES", raising=False)
    with pytest.raises(RuntimeError, match="canonical clone not found"):
        GitWorktreeResolver().resolve(
            repo="owner/repo", pr_number=42, ticket_id=None, explicit_path=None
        )


def test_auto_creation_fetches_resolved_branch_before_worktree_add(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("OMNI_WORKTREES", raising=False)
    canonical = tmp_path / "repo"
    canonical.mkdir()
    branch = Mock(return_value="feature")
    run = Mock()
    monkeypatch.setattr(module, "_resolve_pr_head_branch", branch)
    monkeypatch.setattr(module, "_run_checked", run)
    candidate = GitWorktreeResolver().resolve(
        repo="owner/repo", pr_number=42, ticket_id="TEST-1", explicit_path=None
    )
    assert candidate == tmp_path / "omni_worktrees" / "TEST-1" / "repo"
    assert candidate.parent.is_dir()
    branch.assert_called_once_with("owner/repo", 42)
    assert [(entry.args[0], entry.kwargs) for entry in run.call_args_list] == [
        (["git", "-C", str(canonical), "fetch", "origin", "feature"], {"timeout": 60}),
        (
            [
                "git",
                "-C",
                str(canonical),
                "worktree",
                "add",
                str(candidate),
                "-b",
                "feature",
                "origin/feature",
            ],
            {"timeout": 60},
        ),
    ]


def test_ruff_runs_format_before_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock()
    monkeypatch.setattr(module, "_run_checked", run)
    RuffFixRunner().run(tmp_path)
    assert [(entry.args[0], entry.kwargs) for entry in run.call_args_list] == [
        (["uv", "run", "ruff", "format", "."], {"cwd": tmp_path, "timeout": 120}),
        (
            ["uv", "run", "ruff", "check", "--fix", "."],
            {"cwd": tmp_path, "timeout": 120},
        ),
    ]


def test_git_diff_reads_filters_and_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(
        side_effect=[
            "a.py\n\nb.py\n",
            "2 files changed, 3 insertions(+), 2 deletions(-)",
        ]
    )
    monkeypatch.setattr(module, "_run_checked", run)
    adapter = GitDiffAdapter()
    assert adapter.changed_files(tmp_path) == ["a.py", "b.py"]
    assert adapter.diff_line_count(tmp_path) == 5
    assert [(entry.args[0], entry.kwargs) for entry in run.call_args_list] == [
        (["git", "-C", str(tmp_path), "diff", "--name-only", "HEAD"], {"timeout": 30}),
        (["git", "-C", str(tmp_path), "diff", "--shortstat", "HEAD"], {"timeout": 30}),
    ]


def test_git_commit_and_discard_sequences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(side_effect=["", "", "sha", "", ""])
    monkeypatch.setattr(module, "_run_checked", run)
    adapter = GitDiffAdapter()
    assert adapter.commit_all(tmp_path, "message") == "sha"
    adapter.discard_changes(tmp_path)
    assert [(entry.args[0], entry.kwargs) for entry in run.call_args_list] == [
        (["git", "-C", str(tmp_path), "add", "-A"], {"timeout": 30}),
        (["git", "-C", str(tmp_path), "commit", "-m", "message"], {"timeout": 30}),
        (["git", "-C", str(tmp_path), "rev-parse", "HEAD"], {"timeout": 15}),
        (["git", "-C", str(tmp_path), "checkout", "--", "."], {"timeout": 30}),
        (["git", "-C", str(tmp_path), "clean", "-fd"], {"timeout": 30}),
    ]


@pytest.mark.parametrize(
    ("stdout", "exit_code", "expected"),
    [
        ('{"final_phase":"done"}', 0, PrPolishRunOutcome("done", None)),
        (
            '{"final_phase":"failed","error_message":"gate refused"}',
            1,
            PrPolishRunOutcome("failed", "gate refused"),
        ),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_polish_preserves_terminal_and_always_passes_safety_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    exit_code: int,
    expected: PrPolishRunOutcome,
    dry_run: bool,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], exit_code, stdout, ""))
    monkeypatch.setattr(subprocess, "run", run)
    assert (
        LivePrPolishRunner().run(
            repo="owner/repo",
            pr_number=42,
            ticket_id="TEST-1" if dry_run else None,
            worktree=tmp_path,
            dry_run=dry_run,
        )
        == expected
    )
    argv = run.call_args.args[0]
    assert "--skip-repair-dispatch" in argv
    assert "--no-automerge" in argv
    assert ("--ticket" in argv) is dry_run
    assert ("--no-push" in argv) is dry_run
    assert ("--dry-run" in argv) is dry_run


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("not-json", "non-JSON stdout"),
        ("{}", "missing final_phase"),
        ('{"final_phase": 1}', "missing final_phase"),
    ],
)
def test_polish_rejects_invalid_terminal_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 1, stdout, "stderr")),
    )
    with pytest.raises(RuntimeError, match=message):
        LivePrPolishRunner().run(
            repo="owner/repo",
            pr_number=42,
            ticket_id=None,
            worktree=tmp_path,
            dry_run=False,
        )


@pytest.mark.parametrize("cwd", [None, Path("worktree")])
def test_checked_command_trims_stdout_and_preserves_timeout(
    monkeypatch: pytest.MonkeyPatch,
    cwd: Path | None,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, " sha\n", ""))
    monkeypatch.setattr(subprocess, "run", run)
    assert _run_checked(["tool", "argument"], cwd=cwd, timeout=17) == "sha"
    run.assert_called_once_with(
        ["tool", "argument"],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=17,
    )


def test_checked_command_reports_exit_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 2, "", "tool failed")),
    )
    with pytest.raises(RuntimeError, match=r"command failed \(2\): tool\ntool failed"):
        _run_checked(["tool"])


@pytest.mark.parametrize("branch", ["feature", ""])
def test_head_branch_must_be_nonempty(
    monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    run = Mock(return_value=branch)
    monkeypatch.setattr(module, "_run_checked", run)
    if branch:
        assert _resolve_pr_head_branch("owner/repo", 42) == "feature"
    else:
        with pytest.raises(RuntimeError, match="could not resolve head branch"):
            _resolve_pr_head_branch("owner/repo", 42)
    run.assert_called_once_with(
        [
            "gh",
            "pr",
            "view",
            "42",
            "--repo",
            "owner/repo",
            "--json",
            "headRefName",
            "--jq",
            ".headRefName",
        ],
        timeout=30,
    )


@pytest.mark.parametrize(
    ("shortstat", "expected"),
    [
        ("", 0),
        ("1 file changed, 2 insertions(+)", 2),
        ("1 file changed, 3 deletions(-)", 3),
        ("binary files differ", 0),
        ("insertions(+), deletions(-)", 0),
    ],
)
def test_shortstat_missing_counts(shortstat: str, expected: int) -> None:
    assert _parse_shortstat_lines(shortstat) == expected
