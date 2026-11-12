# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex code-edit run --wall-budget-s`` reaches the loop request (OMN-24006).

Seam tests between the option, the node's overlay and the request the loop runs:
the option wins, else the overlay's optional key, else no limit; an overlay
written before the key existed stays valid; an out-of-range option is refused
by click before any request is built.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from omnimarket.cli import cli_code_edit
from omnimarket.handlers import code_edit_request_builder
from omnimarket.handlers.code_edit_request_builder import build_code_edit_request
from omnimarket.models.model_code_edit_loop_overlay import ModelCodeEditLoopOverlay
from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
)

pytestmark = pytest.mark.unit

CHECK = ModelDeclaredCheck(name="tests", argv=("pytest", "-q"), targets=())


def _overlay(**extra: int) -> ModelCodeEditLoopOverlay:
    return ModelCodeEditLoopOverlay(
        max_turns=20, check_timeout_seconds=300, max_no_change_finishes=2, **extra
    )


def _build(tmp_path: Path, wall_budget_s: int | None) -> ModelDelegatedCodeEditRequest:
    task = tmp_path / "task.md"
    task.write_text("make add return the sum")
    return build_code_edit_request(
        worktree=tmp_path,
        task_file=task,
        writable=("src/*.py",),
        context_paths=(),
        file_list=(),
        cli_checks=(CHECK,),
        formatter=(),
        max_turns=None,
        check_timeout_s=None,
        wall_budget_s=wall_budget_s,
        ticket_contract=None,
        caller_lane=None,
        ticket=None,
    )


def _invoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str
) -> tuple[Result, dict[str, object]]:
    """Run the command up to the request builder, which records its keywords and stops the run."""
    captured: dict[str, object] = {}

    def stop_at_builder(**kwargs: object) -> object:
        captured.update(kwargs)
        raise ValueError("stopped at the builder")

    task = tmp_path / "task.md"
    task.write_text("t")
    monkeypatch.setattr(cli_code_edit, "build_code_edit_request", stop_at_builder)
    result = CliRunner().invoke(
        cli_code_edit.run_command,
        [
            "--worktree",
            str(tmp_path),
            "--task-file",
            str(task),
            "--writable",
            "src/*.py",
            "--check",
            "tests=pytest -q",
            *extra,
        ],
    )
    return result, captured


def test_the_option_reaches_the_request_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, captured = _invoke(tmp_path, monkeypatch, "--wall-budget-s", "300")
    assert "stopped at the builder" in result.output
    assert captured["wall_budget_s"] == 300


def test_an_omitted_option_reaches_the_request_builder_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, captured = _invoke(tmp_path, monkeypatch)
    assert "stopped at the builder" in result.output
    assert captured["wall_budget_s"] is None


@pytest.mark.parametrize("value", ["0", "7201"])
def test_an_out_of_range_option_is_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    result, captured = _invoke(tmp_path, monkeypatch, "--wall-budget-s", value)
    assert result.exit_code == 2
    assert captured == {}


def test_the_option_wins_over_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        code_edit_request_builder,
        "load_code_edit_loop_overlay",
        lambda: _overlay(wall_budget_seconds=600),
    )
    assert _build(tmp_path, 300).wall_budget_seconds == 300


def test_an_omitted_option_takes_the_overlay_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        code_edit_request_builder,
        "load_code_edit_loop_overlay",
        lambda: _overlay(wall_budget_seconds=600),
    )
    assert _build(tmp_path, None).wall_budget_seconds == 600


def test_an_overlay_written_before_the_key_existed_is_valid_and_leaves_no_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _overlay()
    assert overlay.wall_budget_seconds is None
    monkeypatch.setattr(
        code_edit_request_builder, "load_code_edit_loop_overlay", lambda: overlay
    )
    assert _build(tmp_path, None).wall_budget_seconds is None


def test_no_overlay_and_no_option_leave_no_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        code_edit_request_builder, "load_code_edit_loop_overlay", lambda: None
    )
    assert _build(tmp_path, None).wall_budget_seconds is None
