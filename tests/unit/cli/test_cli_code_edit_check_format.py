# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``parse_check`` normalises the lane-brief ``--check`` name (OMN-23954)."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from click import BadParameter
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


def _scripted(
    state: Path, replies: list[dict[str, object]], seen: list[list[str]]
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        run_id = str(uuid.uuid4())
        run_dir = state / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "result.txt").write_text(json.dumps(replies[len(seen) - 1]))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"run_id": run_id}), "")

    return runner


def test_uppercase_name_is_lowercased() -> None:
    check = cli_code_edit.parse_check(
        "PYTEST=python -m pytest tests/unit/cli/test_a.py -k b -q"
    )
    assert check.name == "pytest"
    assert check.targets == ("tests/unit/cli/test_a.py",)


def test_name_is_lowercased() -> None:
    check = cli_code_edit.parse_check("NAME=uv run pytest tests/x.py")
    assert check.name == "name"


def test_space_in_name_becomes_a_dash() -> None:
    check = cli_code_edit.parse_check("Focused Test=pytest tests/x.py")
    assert check.name == "focused-test"


def test_lowercase_name_is_unchanged() -> None:
    check = cli_code_edit.parse_check("tests=uv run pytest tests/x.py -q")
    assert check.name == "tests"


def test_empty_name_is_refused() -> None:
    with pytest.raises(BadParameter):
        cli_code_edit.parse_check("=pytest tests/x.py")


def test_spec_without_equals_is_refused() -> None:
    with pytest.raises(BadParameter):
        cli_code_edit.parse_check("pytest tests/x.py")


def test_command_of_only_spaces_is_refused() -> None:
    with pytest.raises(BadParameter):
        cli_code_edit.parse_check("NAME=   ")


def test_equals_inside_the_command_stays_in_the_argv() -> None:
    check = cli_code_edit.parse_check("PYTEST=python -m pytest tests/x.py -k a=b")
    assert check.argv == ("python", "-m", "pytest", "tests/x.py", "-k", "a=b")


def test_run_accepts_a_lane_brief_check_name(
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
            "PYTEST=true",
            "--state-root",
            str(state),
            "--onex",
            "/bin/onex",
            "--caller-lane",
            "lane-y",
            "--ticket",
            "OMN-23954",
        ],
    )
    assert result.exit_code == 0, result.output
    line = json.loads(result.output.strip().splitlines()[-1])
    assert line["status"] == "accepted"
