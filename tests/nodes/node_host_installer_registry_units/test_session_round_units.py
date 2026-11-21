# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each orchestrator round is a launchd unit the registry installer renders, never a hand-made plist.

Four units run node_session_round_effect, one per round, each with the round's name, prompt, claude
binary and limits in its environment, and the round's heartbeat directory as its state directory.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_log_directory import (
    BREW_PYTHON,
    render,
)

pytestmark = pytest.mark.unit

ENTRY = "src/omnimarket/nodes/node_session_round_effect/__main__.py"
MODULE = "omnimarket.nodes.node_session_round_effect"
# unit -> (cadence in seconds, the workflow script the round's prompt runs)
ROUNDS = {
    "round-fill": (900, "fill-tick.js"),
    "round-integration-pass": (3600, "integ-swarm.js"),
    "round-integration-sweep": (3600, "integ-shards.js"),
    "round-review": (1800, "review-act.js"),
}


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )


@pytest.mark.parametrize("unit", ROUNDS)
def test_each_round_is_a_declared_launchd_unit_of_the_round_node(unit: str) -> None:
    declared = load_units()[unit]
    seconds, script = ROUNDS[unit]
    assert declared["platform"] == "launchd"
    assert declared["kind"] == "registry_module_tick"
    assert declared["label"] == f"ai.omninode.{unit}"
    assert declared["module"] == MODULE
    assert declared["entry_file"] == ENTRY
    assert declared["schedule"]["start_interval"] == seconds
    environment = declared["environment"]
    assert environment["ONEX_ROUND"] == unit
    assert script in environment["ONEX_ROUND_PROMPT"]
    # The round ends itself before the run-limit wrapper would kill it.
    assert 0 < int(environment["ONEX_ROUND_TIMEOUT_S"]) < declared["run_timeout_s"]
    assert declared["evidence_file"] == (
        f".local/state/omni/orchestrator-rounds/{unit}.heartbeat.json"
    )


@pytest.mark.parametrize("unit", ROUNDS)
def test_golden_the_rendered_round_runs_claude_p_with_its_heartbeat_directory(
    unit: str, tmp_path: Path
) -> None:
    seconds, script = ROUNDS[unit]
    plist = render(tmp_path, unit, ENTRY)
    arguments = plist["ProgramArguments"]
    environment = plist["EnvironmentVariables"]
    assert plist["Label"] == f"ai.omninode.{unit}"
    assert plist["StartInterval"] == seconds
    assert plist["RunAtLoad"] is False
    assert arguments[arguments.index("-m") + 1] == MODULE
    heartbeat_dir = tmp_path / "home/.local/state/omni/orchestrator-rounds"
    assert arguments[arguments.index("--state-dir") + 1] == str(heartbeat_dir)
    assert environment["ONEX_ROUND"] == unit
    assert environment["ONEX_ROUND_CLAUDE"] == str(tmp_path / "home/.local/bin/claude")
    assert shlex.split(environment["ONEX_ROUND_CLAUDE_ARGS"])[:2] == ["--model", "opus"]
    prompt = environment["ONEX_ROUND_PROMPT"]
    assert f"/.onex_state/orchestrator-rounds/workflows/{script}" in prompt
    assert "{registry_root}" not in prompt
    assert "{home}" not in environment["ONEX_ROUND_CLAUDE"]
