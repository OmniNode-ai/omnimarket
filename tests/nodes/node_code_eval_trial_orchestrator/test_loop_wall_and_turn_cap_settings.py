# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26656 -- the loop's wall budget and turn cap are declared overlay values.

A trial overlay with no ``loop_wall_budget_seconds`` refuses to load (no default, so no silent
300 s); a turn cap of 100 loads; the draft unit carries both to the loop request.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result
from pydantic import ValidationError

from omnimarket.cli import cli_code_edit
from omnimarket.models.code_eval_trial.model_code_eval_overlay import (
    ModelCodeEvalRunnerOverlay,
)
from omnimarket.models.model_delegated_code_edit import (
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    Bus,
    leg_config,
    make_bus,
    overlay,
    run_request,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_code_eval_trial_orchestrator/overlay.example.yaml"
)


def test_overlay_without_a_loop_wall_budget_refuses_to_load() -> None:
    values = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del values["loop_wall_budget_seconds"]
    with pytest.raises(ValidationError, match="loop_wall_budget_seconds"):
        ModelCodeEvalRunnerOverlay.model_validate(values)


def test_example_overlay_declares_a_3600_second_loop_wall_budget() -> None:
    values = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert (
        ModelCodeEvalRunnerOverlay.model_validate(values).loop_wall_budget_seconds
        == 3600
    )


def test_overlay_without_a_turn_cap_refuses_to_load() -> None:
    values = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del values["max_turns"]
    with pytest.raises(ValidationError, match="max_turns"):
        ModelCodeEvalRunnerOverlay.model_validate(values)


def test_a_turn_cap_of_100_loads_and_101_refuses() -> None:
    assert overlay(max_turns=100).max_turns == 100
    with pytest.raises(ValidationError, match="max_turns"):
        overlay(max_turns=101)


def test_the_loop_request_accepts_100_turns_and_a_3600_second_wall() -> None:
    request = ModelDelegatedCodeEditRequest(
        correlation_id="11111111-1111-4111-8111-111111111111",
        task="t",
        workspace_root="/tmp/w",
        writable_globs=("a.py",),
        checks=(ModelDeclaredCheck(name="c", argv=("true",)),),
        max_turns=100,
        wall_budget_seconds=3600,
    )
    assert (request.max_turns, request.wall_budget_seconds) == (100, 3600)


def test_the_draft_unit_carries_the_overlays_wall_budget_and_turn_cap() -> None:
    bus: Bus = make_bus(config=leg_config(max_turns=100, loop_wall_budget_seconds=3600))
    bus.send(run_request(2, 2))
    bus.drain()
    assert bus.units
    for unit in bus.units:
        assert unit.code_draft is not None
        assert unit.code_draft.max_turns == 100
        assert unit.code_draft.wall_budget_seconds == 3600


def _invoke_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_turns: str
) -> tuple[Result, dict[str, object]]:
    """``onex code-edit run --max-turns`` up to the request builder, which records its keywords."""
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
            "--max-turns",
            max_turns,
        ],
    )
    return result, captured


def test_the_code_edit_cli_takes_a_turn_cap_of_100_and_refuses_101(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The option was click.IntRange(1, 40): ``--max-turns 100`` was refused before any request."""
    result, captured = _invoke_cli(tmp_path, monkeypatch, "100")
    assert "stopped at the builder" in result.output
    assert captured["max_turns"] == 100
    refused, none_built = _invoke_cli(tmp_path, monkeypatch, "101")
    assert refused.exit_code == 2
    assert none_built == {}
