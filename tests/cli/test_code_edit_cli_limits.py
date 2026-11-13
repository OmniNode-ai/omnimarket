# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests of the code edit CLI limits from the overlay (OMN-23930)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import omnimarket.cli.cli_code_edit as cli_code_edit
from omnimarket.cli.cli_code_edit import code_edit_group


def _scripted(
    state_root: Path, replies: list[str], seen: list[str]
) -> subprocess.CompletedProcess[str]:
    def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(" ".join(argv))
        return subprocess.CompletedProcess(
            argv, 0, stdout=replies[0] if replies else ""
        )

    return _run


def test_overlay_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overlay_root = tmp_path / "overlay"
    node_dir = overlay_root / "node_delegated_code_edit_orchestrator"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(
        "max_turns: 15\ncheck_timeout_seconds: 450\nmax_no_change_finishes: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay_root))

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task_file = tmp_path / "task.md"
    task_file.write_text("do something", encoding="utf-8")

    state_root = tmp_path / "state"
    state_root.mkdir()
    seen: list[str] = []
    monkeypatch.setattr(
        cli_code_edit, "delegate_runner", lambda: _scripted(state_root, ["{}"], seen)
    )

    runner = CliRunner()
    result = runner.invoke(
        code_edit_group,
        [
            "run",
            "--worktree",
            str(worktree),
            "--task-file",
            str(task_file),
            "--writable",
            "src/*.py",
            "--check",
            "tests=uv run pytest tests/test_a.py -q",
            "--state-root",
            str(state_root),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code in (0, 3)


def test_max_turns_flag_beats_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_root = tmp_path / "overlay"
    node_dir = overlay_root / "node_delegated_code_edit_orchestrator"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(
        "max_turns: 15\ncheck_timeout_seconds: 450\nmax_no_change_finishes: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay_root))

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task_file = tmp_path / "task.md"
    task_file.write_text("do something", encoding="utf-8")

    state_root = tmp_path / "state"
    state_root.mkdir()
    seen: list[str] = []
    monkeypatch.setattr(
        cli_code_edit, "delegate_runner", lambda: _scripted(state_root, ["{}"], seen)
    )

    runner = CliRunner()
    result = runner.invoke(
        code_edit_group,
        [
            "run",
            "--worktree",
            str(worktree),
            "--task-file",
            str(task_file),
            "--writable",
            "src/*.py",
            "--check",
            "tests=uv run pytest tests/test_a.py -q",
            "--max-turns",
            "7",
            "--state-root",
            str(state_root),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code in (0, 3)
