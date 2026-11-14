# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23734: the hourly check's goal check, contract to bus to typed result, and the parity run for CI.

node_throughput_goal_check_compute carries its parity tests beside it (src/.../tests, the ticket's AC1
command); no CI pytest invocation collects src/, so this module imports them to run them here, and drives
a recorded tick through the registered contract on the in-memory bus.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_throughput_goal_check_compute as node_package
from omnimarket.nodes.node_throughput_goal_check_compute.models.model_throughput_goal_check import (
    ModelThroughputGoalCheckResult,
)
from omnimarket.nodes.node_throughput_goal_check_compute.tests.test_goal_check_parity import (  # noqa: F401
    _TICKS,
    test_a_changed_scope_list_turns_the_parity_red,
    test_a_planted_change_to_one_input_turns_the_parity_red,
    test_an_unread_admission_record_is_named_and_the_lists_alone_decide,
    test_an_unread_scope_is_one_unknown_finding_and_no_record,
    test_goal_line_findings_and_record_match_the_script_parity,
    test_the_recording_covers_each_branch_of_the_check,
    test_the_recording_holds_twenty_ticks,
    test_the_same_tick_again_reads_nothing_changed,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit


def _contract(node_dir: Path) -> dict[str, Any]:
    return dict(yaml.safe_load((node_dir / "contract.yaml").read_text()))


async def _run(node_dir: Path, tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=node_dir / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.throughput-goal-check-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.throughput-goal-check-completed.v1"


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract(NODE_DIR)
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeThroughputGoalCheckCompute, handler_type)
    declared = getattr(
        importlib.import_module(contract["output_model"]["module"]),
        contract["output_model"]["name"],
    )
    assert declared is ModelThroughputGoalCheckResult
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_throughput_goal_check_compute"].load() is node_package


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1, 6, 11, 19])
async def test_golden_chain_over_the_bus_matches_the_recorded_tick(
    tmp_path: Path, index: int
) -> None:
    tick = _TICKS[index]
    payload = {k: v for k, v in tick.items() if k != "expected"}
    runtime = await _run(NODE_DIR, tmp_path, payload)
    result = runtime.handler_result
    assert isinstance(result, ModelThroughputGoalCheckResult)
    assert result.goal_line == tick["expected"]["goal_line"]
    assert result.record == tick["expected"]["record"]
    assert (
        ModelThroughputGoalCheckResult.model_validate_json(result.model_dump_json())
        == result
    )
