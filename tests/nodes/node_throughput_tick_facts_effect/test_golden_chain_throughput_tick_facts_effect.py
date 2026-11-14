# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23734: the hourly check's facts read, contract to bus to typed facts, and the parity run for CI.

The parity, golden and error-chain tests live beside the node (src/.../tests, the ticket's AC2 command);
no CI pytest invocation collects src/, so this module imports them to run them here, and drives a recorded
tick through the registered contract on the in-memory bus against real files and scripted programs.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_throughput_tick_facts_effect as node_package
from omnimarket.nodes.node_throughput_tick_facts_effect.models.model_throughput_tick_facts import (
    ModelThroughputTickFactsResult,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.tests.test_tick_facts_parity import (
    _build,
    _tick,
    short_root,
    test_a_planted_change_to_one_file_turns_the_parity_red,
    test_error_chain_every_unreadable_source_is_its_error_text_not_a_raise,
    test_error_chain_no_floors_path_is_a_floors_error,
    test_facts_equal_what_the_script_read_parity,
    test_golden_a_scripted_host_gives_the_loaded_controller_facts,
    test_the_recording_holds_twenty_ticks_and_every_branch,
)
from tests.runtime_local_compat import RuntimeLocal

__all__ = [
    "_build",
    "_tick",
    "short_root",
    "test_a_planted_change_to_one_file_turns_the_parity_red",
    "test_error_chain_every_unreadable_source_is_its_error_text_not_a_raise",
    "test_error_chain_no_floors_path_is_a_floors_error",
    "test_facts_equal_what_the_script_read_parity",
    "test_golden_a_scripted_host_gives_the_loaded_controller_facts",
    "test_the_recording_holds_twenty_ticks_and_every_branch",
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
COMMAND_TOPIC = "onex.cmd.omnimarket.throughput-tick-facts-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.throughput-tick-facts-read.v1"


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract(NODE_DIR)
    assert contract["node_type"] == "effect"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeThroughputTickFactsEffect, handler_type)
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_throughput_tick_facts_effect"].load() is node_package


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_reads_the_recorded_facts(
    short_root: Path,
    tmp_path: Path,
) -> None:
    index = 3
    tick = _tick(index, short_root)
    request = _build(tick, short_root)
    runtime = await _run(NODE_DIR, tmp_path, json.loads(request.model_dump_json()))
    result = runtime.handler_result
    assert isinstance(result, ModelThroughputTickFactsResult)
    assert result.facts.model_dump(exclude_none=True) == {
        k: v for k, v in tick["expected"]["facts"].items() if v is not None
    }
