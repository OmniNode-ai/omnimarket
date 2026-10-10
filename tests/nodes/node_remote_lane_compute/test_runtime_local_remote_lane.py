# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every remote-lane decision runs through its packaged contract over the bus (OMN-20669).

The remote-lane runner calls these nodes the way ``onex node <name> --input <file>``
does: the runtime loads the contract, seeds the payload into the handler's input
model, publishes it on the command topic and reads the terminal payload. A decision
whose handler shares a command topic with another operation is unreachable that
way, because the runtime seeds only the first handler's model. Each decision here
must complete with the handler's own answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from tests.runtime_local_compat import RuntimeLocal

_NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
_SHA = "0123456789abcdef0123456789abcdef01234567"

_CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        "node_remote_lane_compute",
        {
            "engine": "claude_sonnet",
            "readings": [
                {
                    "name": "h201",
                    "engines": ["claude_sonnet"],
                    "lane_slots": 1,
                    "lane_cap": 4,
                    "placed": 1,
                },
                {
                    "name": "h202",
                    "engines": ["claude_sonnet"],
                    "lane_slots": 2,
                    "lane_cap": 4,
                },
            ],
        },
        {"host": "h202", "engine": "claude_sonnet", "local": False},
    ),
    (
        # An unreadable host has no rank (the reader's -inf, which the bus envelope
        # cannot carry as a number): it travels as null and ranks below every host.
        "node_remote_lane_compute",
        {
            "engine": "codex",
            "readings": [
                {"name": "h101", "rank_free": None},
                {"name": "h202", "rank_free": 7.19, "mem_avail_gb": 37.3},
            ],
        },
        {"host": "h202", "engine": "codex", "local": False},
    ),
    (
        # Dispatch-venv drift (OMN-20862): no host only because the hashes differ is a
        # VENV_DRIFT answer on the decided event, not a bare no-host.
        "node_remote_lane_compute",
        {
            "engine": "claude_sonnet",
            "launching_venv_hash": "a1" * 32,
            "readings": [
                {
                    "name": "h201",
                    "engines": ["claude_sonnet"],
                    "lane_slots": 2,
                    "lane_cap": 4,
                    "dispatch_venv_hash": "b2" * 32,
                },
            ],
        },
        {
            "host": None,
            "engine": "claude_sonnet",
            "outcome": "VENV_DRIFT",
            "venv_drift": {
                "launching_hash": "a1" * 32,
                "hosts": [{"host": "h201", "hash": "b2" * 32}],
            },
            "reconcile": {
                "script": "omnibase_infra/scripts/reconcile-workspace-venvs.sh",
                "hosts": ["h201"],
                "target_hash": "a1" * 32,
            },
        },
    ),
    (
        "node_remote_lane_close_compute",
        {
            "engine_exit_code": 0,
            "final_message": "LANE_RESULT outcome=handed-off\n"
            "DELEGATION delegated=0 reason=read-only-lane",
        },
        {
            "outcome": "handed-off",
            "problem": None,
            "declared_outcome": "handed-off",
        },
    ),
    (
        "node_remote_lane_effect",
        {"repo": "omnimarket", "owner": "OmniNode-ai", "sha": _SHA},
        {"sha": _SHA, "error": None},
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize(("node", "payload", "expected"), _CASES)
def test_decision_completes_through_its_contract(
    tmp_path: Path, node: str, payload: dict[str, Any], expected: dict[str, Any]
) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload))
    state = tmp_path / "state"

    runtime = RuntimeLocal(
        workflow_path=_NODES / node / "contract.yaml",
        state_root=state,
        input_path=input_path,
        timeout=30,
    )

    assert runtime.run() == EnumWorkflowResult.COMPLETED
    written = json.loads((state / "workflow_result.json").read_text())
    # An event-driven contract answers on its terminal topic; one with no event_bus
    # block (the effect node, hosted by a serve process) answers from its handler.
    terminal = written.get("terminal_payload") or written["handler_result"]
    assert {key: terminal[key] for key in expected} == expected


@pytest.mark.unit
def test_each_compute_contract_routes_one_operation_on_its_own_topic() -> None:
    """One command topic per decision: a shared topic leaves an operation unseeded."""
    topics: dict[str, str] = {}
    for node in ("node_remote_lane_compute", "node_remote_lane_close_compute"):
        contract = yaml.safe_load((_NODES / node / "contract.yaml").read_text())
        handlers = contract["handler_routing"]["handlers"]
        assert len(handlers) == 1, node
        (topic,) = contract["event_bus"]["subscribe_topics"]
        assert topic not in topics, (node, topics.get(topic))
        topics[topic] = node
