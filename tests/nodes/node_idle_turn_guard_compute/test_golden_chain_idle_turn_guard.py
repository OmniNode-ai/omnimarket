# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded Stop-turn decisions through the registered compute contract and local bus."""

from __future__ import annotations

import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_idle_turn_guard_compute as node_package
from omnimarket.models.idle_turn_guard import (
    EnumIdleTurnGuardVerdict,
    ModelIdleTurnGuardDecision,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.idle-turn-guard-decision-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.idle-turn-guard-decided.v1"


def test_contract_routes_the_decision_back_to_the_runtime_caller() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["runtime_dispatch"]["terminal_events"]["success"] == TERMINAL_TOPIC
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_idle_turn_guard_compute"].load() is node_package


def _runtime(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    state_root = tmp_path / "state"
    state_root.mkdir()
    return RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=state_root,
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )


@pytest.mark.parametrize(
    ("facts", "verdict"),
    [
        ({}, EnumIdleTurnGuardVerdict.REFUSE),
        (
            {
                "holds_in_force": [
                    {
                        "id": "operator-stop",
                        "kind": "stop",
                        "stamp": "2026-11-04T21:01:00Z",
                    }
                ]
            },
            EnumIdleTurnGuardVerdict.HELD,
        ),
        (
            {"lane_refusing_hosts": ["host-r"]},
            EnumIdleTurnGuardVerdict.PASS,
        ),
        (
            {
                "window_start": "2026-11-04T21:00:00Z",
                "runner_receipts": [
                    {
                        "lane": "background-send",
                        "state": "placing",
                        "at": "2026-11-04T21:02:00Z",
                    }
                ],
            },
            EnumIdleTurnGuardVerdict.PASS,
        ),
    ],
    ids=["idle-refused", "operator-stop", "refusing-host", "background-send"],
)
@pytest.mark.asyncio
async def test_golden_chain_decides_recorded_stop_turns(
    tmp_path: Path, facts: dict[str, Any], verdict: EnumIdleTurnGuardVerdict
) -> None:
    runtime = _runtime(
        tmp_path,
        {
            "headroom_lines": [
                "MISSING lab-headroom:host-r | free slots 16 | FIX: dispatch"
            ],
            "facts": facts,
            "now": "2026-11-04T21:04:00Z",
        },
    )
    assert await runtime.run_async() is EnumWorkflowResult.COMPLETED
    result = runtime.handler_result
    assert isinstance(result, ModelIdleTurnGuardDecision)
    assert result.verdict is verdict
    if verdict is EnumIdleTurnGuardVerdict.HELD:
        assert result.hold_ids == ("operator-stop",)
    if facts.get("runner_receipts"):
        assert result.receipt_lanes == ("background-send",)


@pytest.mark.asyncio
async def test_error_chain_refuses_malformed_facts_without_a_result(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path,
        {"facts": {"holds_in_force": "stop"}, "now": "2026-11-04T21:04:00Z"},
    )
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
