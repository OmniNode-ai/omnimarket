# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex code-edit run`` binds the loop and reports one line (OMN-20290)."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.cli import cli_code_edit

pytestmark = pytest.mark.unit


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "src").mkdir(parents=True)
    (root / "src" / "m.py").write_text("VALUE = 0\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


def _scripted(state: Path, replies: list[dict[str, object]], seen: list[list[str]]):  # type: ignore[no-untyped-def]
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        run_id = str(uuid.uuid4())
        run_dir = state / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "result.txt").write_text(json.dumps(replies[len(seen) - 1]))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"run_id": run_id}), "")

    return runner


@pytest.mark.parametrize(
    ("formatters", "expected"),
    [
        ((), []),
        (("ruff format",), [["ruff", "format"]]),
        (
            ("ruff check --select I --fix", "ruff format"),
            [["ruff", "check", "--select", "I", "--fix"], ["ruff", "format"]],
        ),
    ],
)
def test_run_accepts_when_the_declared_check_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    formatters: tuple[str, ...],
    expected: list[list[str]],
) -> None:
    tree = _tree(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("set VALUE to 1")
    state = tmp_path / "state"
    seen: list[list[str]] = []
    replies: list[dict[str, object]] = [
        {
            "actions": [
                {
                    "tool": "edit",
                    "file_path": "src/m.py",
                    "old_string": "VALUE = 0",
                    "new_string": "VALUE = 1",
                },
                {"tool": "finish", "summary": "VALUE is 1"},
            ]
        },
    ]
    monkeypatch.setattr(
        cli_code_edit, "delegate_runner", lambda: _scripted(state, replies, seen)
    )
    result = CliRunner().invoke(
        cli_code_edit.code_edit_group,
        [
            "run",
            "--worktree",
            str(tree),
            "--task-file",
            str(task),
            "--writable",
            "src/*.py",
            "--check",
            "value=grep -q 'VALUE = 1' src/m.py",
            "--state-root",
            str(state),
            "--onex",
            "/bin/onex",
            "--caller-lane",
            "lane-y",
            "--ticket",
            "OMN-20290",
            *[arg for step in formatters for arg in ("--formatter", step)],
        ],
    )
    assert result.exit_code == 0, result.output
    line = json.loads(result.output.strip().splitlines()[-1])
    assert line["status"] == "accepted"
    assert line["changed_paths"] == ["src/m.py"]
    assert len(line["delegate_run_ids"]) == 1
    argv = seen[0]
    assert argv[argv.index("--lane") + 1] == "dev"
    assert argv[argv.index("--caller-lane") + 1] == "lane-y"
    receipt = json.loads(
        (state / "runs" / line["loop_run_id"] / "loop_receipt.json").read_text()
    )
    assert receipt["request"]["formatter"] == expected


def test_run_exits_3_when_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("set VALUE to 1")
    state = tmp_path / "state"
    seen: list[list[str]] = []
    replies: list[dict[str, object]] = [{"actions": [{"tool": "ls"}]}]
    monkeypatch.setattr(
        cli_code_edit, "delegate_runner", lambda: _scripted(state, replies, seen)
    )
    result = CliRunner().invoke(
        cli_code_edit.code_edit_group,
        [
            "run",
            "--worktree",
            str(tree),
            "--task-file",
            str(task),
            "--writable",
            "src/*.py",
            "--check",
            "value=grep -q 'VALUE = 1' src/m.py",
            "--state-root",
            str(state),
            "--onex",
            "/bin/onex",
            "--max-turns",
            "1",
            "--delegate-in-process",
        ],
    )
    assert result.exit_code == 3, result.output
    assert (
        json.loads(result.output.strip().splitlines()[-1])["status"]
        == "budget_exhausted"
    )
    assert seen[0][seen[0].index("--locus") + 1] == "in-process"


def test_run_refuses_without_a_check(tmp_path: Path) -> None:
    task = tmp_path / "task.md"
    task.write_text("x")
    result = CliRunner().invoke(
        cli_code_edit.code_edit_group,
        [
            "run",
            "--worktree",
            str(tmp_path),
            "--task-file",
            str(task),
            "--writable",
            "src/*.py",
        ],
    )
    assert result.exit_code == 1
    assert "at least one --check" in result.output


def test_parse_check_splits_argv_and_finds_test_targets() -> None:
    check = cli_code_edit.parse_check("tests=uv run pytest tests/test_a.py -q")
    assert check.argv == ("uv", "run", "pytest", "tests/test_a.py", "-q")
    assert check.targets == ("tests/test_a.py",)


def test_resume_delegate_failed_loop_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("set VALUE to 1")
    state = tmp_path / "state"
    seen: list[list[str]] = []
    scripted = _scripted(
        state,
        [
            {
                "actions": [
                    {"tool": "write", "file_path": "src/m.py", "content": "VALUE = 1\n"}
                ]
            },
        ],
        seen,
    )
    calls = 0

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls > 1:
            return subprocess.CompletedProcess(argv, 1, "", "delegate failed")
        return scripted(argv)

    monkeypatch.setattr(cli_code_edit, "delegate_runner", lambda: runner)
    cli = CliRunner()
    first = cli.invoke(
        cli_code_edit.code_edit_group,
        [
            "run",
            "--worktree",
            str(tree),
            "--task-file",
            str(task),
            "--writable",
            "src/*.py",
            "--check",
            "value=grep -q 'VALUE = 1' src/m.py",
            "--state-root",
            str(state),
            "--onex",
            "/bin/onex",
            "--max-turns",
            "3",
        ],
    )
    assert first.exit_code == 3, first.output
    result = json.loads(first.output)
    assert result["status"] == "delegate_failed"
    assert result["resumable"]
    correlation = result["loop_run_id"]
    resumed_seen: list[list[str]] = []
    monkeypatch.setattr(
        cli_code_edit,
        "delegate_runner",
        lambda: _scripted(
            state,
            [{"actions": [{"tool": "finish", "summary": "VALUE is 1"}]}],
            resumed_seen,
        ),
    )
    resumed = cli.invoke(
        cli_code_edit.code_edit_group,
        [
            "run",
            "--resume",
            correlation,
            "--state-root",
            str(state),
            "--onex",
            "/bin/onex",
            "--max-turns",
            "1",  # Resume uses the original request's cap.
        ],
    )
    assert resumed.exit_code == 0, resumed.output
    result = json.loads(resumed.output)
    assert result["status"] == "accepted"
    assert result["turns"] == 2
    loop_dir = state / "runs" / correlation
    assert json.loads((loop_dir / "loop_receipt.json").read_text())["resumes"] == 1
    assert (loop_dir / "loop_receipt.1.json").exists()
    refused = cli.invoke(
        cli_code_edit.code_edit_group,
        [
            "run",
            "--resume",
            correlation,
            "--state-root",
            str(state),
            "--onex",
            "/bin/onex",
        ],
    )
    assert refused.exit_code == 1
    assert "status accepted" in refused.output
    assert "2 turns used of 3" in refused.output
    assert len(resumed_seen) == 1


@pytest.mark.parametrize(
    "flag",
    [
        "--worktree",
        "--request",
        "--task-file",
        "--writable",
        "--context",
        "--file-list",
        "--check",
        "--formatter",
        "--new-correlation",
    ],
)
def test_resume_refuses_conflicting_flags(tmp_path: Path, flag: str) -> None:
    file = tmp_path / "request.json"
    file.write_text("{}")
    value = (
        str(tmp_path)
        if flag == "--worktree"
        else str(file)
        if flag in ("--request", "--task-file", "--file-list")
        else "x"
    )
    args = ["run", "--resume", str(uuid.uuid4()), flag]
    if flag != "--new-correlation":
        args.append(value)
    result = CliRunner().invoke(cli_code_edit.code_edit_group, args)
    assert result.exit_code == 1
    assert f"--resume conflicts with {flag}" in result.output


@pytest.mark.parametrize("text", [None, "garbage", "[]", "{}", '{"request": {}}'])
def test_resume_unreadable_receipt_exits_one(tmp_path: Path, text: str | None) -> None:
    correlation = str(uuid.uuid4())
    if text is not None:
        loop_dir = tmp_path / "runs" / correlation
        loop_dir.mkdir(parents=True)
        (loop_dir / "loop_receipt.json").write_text(text)
    result = CliRunner().invoke(
        cli_code_edit.code_edit_group,
        [
            "run",
            "--resume",
            correlation,
            "--state-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "unreadable receipt" in result.output


def test_resume_requires_uuid() -> None:
    result = CliRunner().invoke(
        cli_code_edit.code_edit_group, ["run", "--resume", "invalid"]
    )
    assert result.exit_code == 2
    assert "UUID" in result.output


def test_file_list_flag_reads_one_path_per_line(tmp_path: Path) -> None:
    listing = tmp_path / "files.txt"
    listing.write_text("# the task's files\nsrc/a.py\n\n  src/b.py  \nsrc/a.py\n")
    assert cli_code_edit.read_file_list(listing) == ("src/a.py", "src/b.py")
    assert cli_code_edit.read_file_list(None) == ()


def test_run_carries_the_file_list_into_the_request_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("set VALUE to 1")
    listing = tmp_path / "files.txt"
    listing.write_text("src/m.py\n")
    state = tmp_path / "state"
    seen: list[list[str]] = []
    replies: list[dict[str, object]] = [{"actions": [{"tool": "finish"}]}]
    monkeypatch.setattr(
        cli_code_edit, "delegate_runner", lambda: _scripted(state, replies, seen)
    )
    result = CliRunner().invoke(
        cli_code_edit.code_edit_group,
        [
            "run",
            "--worktree",
            str(tree),
            "--task-file",
            str(task),
            "--writable",
            "src/*.py",
            "--file-list",
            str(listing),
            "--check",
            "true=true",
            "--state-root",
            str(state),
            "--onex",
            "/bin/onex",
        ],
    )
    line = json.loads(result.output.strip().splitlines()[-1])
    receipt = json.loads(
        (state / "runs" / line["loop_run_id"] / "loop_receipt.json").read_text()
    )
    assert receipt["request"]["file_list"] == ["src/m.py"]


def test_run_refuses_a_file_list_that_leaves_the_worktree(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("x")
    listing = tmp_path / "files.txt"
    listing.write_text("../outside.py\n")
    result = CliRunner().invoke(
        cli_code_edit.code_edit_group,
        [
            "run",
            "--worktree",
            str(tree),
            "--task-file",
            str(task),
            "--writable",
            "src/*.py",
            "--file-list",
            str(listing),
            "--check",
            "true=true",
        ],
    )
    assert result.exit_code == 1
    assert "invalid request" in result.output
