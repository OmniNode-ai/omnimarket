# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A publish step can run with its own variables, and the floor step names none (OMN-23669).

The unit carries one ledger read command for every step. The floor step used to carry ``ONEX_LEDGER_RECORD: file`` (OMN-23606)
because its 14-day window read was never answered by the bus source (the answer is far past the read node's bound) and the reader
fell back to the ledger file after a 5 to 14 s refusal. The floor effect now reads the window from the read node page by page
(OMN-23669), so the step needs no ledger record of its own and the value is gone from the contract.

Golden chain: the contract's floor step declares no variable, and a step that names a variable runs its program with it. Error
chain (the failing controls): the same step without the variable runs without it; a lower-case name and a variable on a step that
runs no program are refused by the model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelPublishStep,
)

pytestmark = pytest.mark.unit

CHECK = "import os, sys; sys.exit(0 if os.environ.get('ONEX_LEDGER_RECORD') == 'file' else 3)"


def run(tmp_path: Path, step: ModelPublishStep) -> tuple[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    result = HandlerFactoryFloorPublish()._run_step(
        step,
        home,
        ModelFactoryFloorPublishOverlay(
            ssh_target="lab-web",
            remote_dir=str(home / "site"),
            serve_url="http://example.invalid:8088/",
            bus_lane="lane-x",
            history_root=str(home / "kept"),
            owner_login="owner@example.invalid",
        ),
        "CLAUDE_PLUGIN_ROOT",
        {},
    )
    return result.status, result.detail


def check_step(**kw: object) -> ModelPublishStep:
    return ModelPublishStep.model_validate(
        {
            "name": "check",
            "kind": "command",
            "required": False,
            "timeout_seconds": 30,
            "argv": [sys.executable, "-c", CHECK],
            **kw,
        }
    )


def test_golden_the_floor_step_names_no_ledger_record() -> None:
    floor = next(s for s in load_runtime().steps if s.name == "factory_floor")
    assert floor.required
    assert floor.environment == {}


def test_golden_a_step_runs_its_program_with_the_variables_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LEDGER_RECORD", "bus")
    assert run(tmp_path, check_step(environment={"ONEX_LEDGER_RECORD": "file"})) == (
        "ok",
        "",
    )


def test_error_control_without_the_variable_the_step_runs_with_the_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LEDGER_RECORD", "bus")
    assert run(tmp_path, check_step()) == ("failed", "exit 3")


def test_error_no_step_changes_its_ledger_record() -> None:
    named = [s.name for s in load_runtime().steps if s.environment]
    assert named == []


def test_error_a_lower_case_variable_name_is_refused() -> None:
    with pytest.raises(ValueError, match="upper-case"):
        check_step(environment={"onex_ledger_record": "file"})


def test_error_a_step_that_runs_no_program_takes_no_environment() -> None:
    with pytest.raises(ValueError, match="takes no environment"):
        ModelPublishStep.model_validate(
            {
                "name": "copy_it",
                "kind": "copy",
                "required": False,
                "timeout_seconds": 5,
                "source_relpath": "a.json",
                "output_relpath": "b.json",
                "environment": {"ONEX_LEDGER_RECORD": "file"},
            }
        )
