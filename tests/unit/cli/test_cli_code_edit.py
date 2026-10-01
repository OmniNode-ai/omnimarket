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


def test_run_accepts_when_the_declared_check_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
