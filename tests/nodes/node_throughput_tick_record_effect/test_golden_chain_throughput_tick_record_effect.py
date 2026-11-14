# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23734: the hourly check's record step, contract to bus to the rows on disk, and the parity run for CI.

The parity, golden and error-chain tests live beside the node (src/.../tests, the ticket's AC2 command);
no CI pytest invocation collects src/, so this module imports them to run them here, and drives a recorded
tick through the registered contract on the in-memory bus into a real state directory.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_throughput_tick_record_effect as node_package
from omnimarket.nodes.node_throughput_tick_record_effect.models.model_throughput_tick_record import (
    ModelThroughputTickRecordResult,
)
from omnimarket.nodes.node_throughput_tick_record_effect.tests.test_tick_record_parity import (
    _ALL,
    _TICKS,
    test_a_planted_change_to_one_input_turns_the_parity_red,
    test_a_verdict_that_names_no_repo_is_refused_at_the_model,
    test_error_chain_an_unmakeable_state_directory_is_the_finding_not_a_raise,
    test_error_chain_each_unwritable_row_is_a_finding_and_the_others_still_land,
    test_golden_one_tick_writes_the_five_rows,
    test_heartbeat_open_count_and_repo_status_rows_match_the_script_on_twenty_ticks_parity,
    test_the_history_keeps_twelve_points,
    test_the_in_memory_store_keeps_the_same_rows_as_the_file_store_parity,
    test_the_recording_holds_twenty_ticks,
)
from tests.runtime_local_compat import RuntimeLocal

__all__ = [
    "_ALL",
    "_TICKS",
    "test_a_planted_change_to_one_input_turns_the_parity_red",
    "test_a_verdict_that_names_no_repo_is_refused_at_the_model",
    "test_error_chain_an_unmakeable_state_directory_is_the_finding_not_a_raise",
    "test_error_chain_each_unwritable_row_is_a_finding_and_the_others_still_land",
    "test_golden_one_tick_writes_the_five_rows",
    "test_heartbeat_open_count_and_repo_status_rows_match_the_script_on_twenty_ticks_parity",
    "test_the_history_keeps_twelve_points",
    "test_the_in_memory_store_keeps_the_same_rows_as_the_file_store_parity",
    "test_the_recording_holds_twenty_ticks",
]

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
COMMAND_TOPIC = "onex.cmd.omnimarket.throughput-tick-record-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.throughput-tick-record-completed.v1"


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract(NODE_DIR)
    assert contract["node_type"] == "effect"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeThroughputTickRecordEffect, handler_type)
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_throughput_tick_record_effect"].load() is node_package


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_keeps_the_recorded_rows(
    tmp_path: Path,
) -> None:
    tick = _TICKS[0]
    state = tmp_path / "tick-state"
    runtime = await _run(
        NODE_DIR, tmp_path, {**tick["request"], "state_dir": str(state)}
    )
    result = runtime.handler_result
    assert isinstance(result, ModelThroughputTickRecordResult)
    assert result.ok
    for name in _ALL:
        expected = tick["files_after"].get(name)
        target = state / name
        assert (
            target.read_text(encoding="utf-8") if target.exists() else None
        ) == expected


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_names_an_unwritable_state_directory(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    runtime = await _run(
        NODE_DIR,
        tmp_path,
        {**_TICKS[0]["request"], "state_dir": str(blocker / "state")},
    )
    result = runtime.handler_result
    assert isinstance(result, ModelThroughputTickRecordResult)
    assert result.ok is False
    assert {f.loop for f in result.findings} == {
        "open-trend-history",
        "repo-status-units",
        "goal-check-record",
    }
