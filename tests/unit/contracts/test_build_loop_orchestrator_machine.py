# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build-loop orchestrator declares a walkable machine and typed terminal events."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.validation.validator_contract_walker import walk_contracts

_REPO = Path(__file__).resolve().parents[3]
_CONTRACT = _REPO / "src/omnimarket/nodes/node_build_loop_orchestrator/contract.yaml"
_NODE = "node_build_loop_orchestrator"

# The phases HandlerBuildLoop's mode sequences (node_build_loop.models.model_loop_state)
# can occupy; the contract machine must declare every one of them.
_PHASES = {
    "IDLE",
    "CLOSING_OUT",
    "VERIFYING",
    "FILLING",
    "CLASSIFYING",
    "BUILDING",
    "RELEASING",
    "DEPLOYING",
    "POST_VERIFY",
    "COMPLETE",
    "FAILED",
}


def _contract() -> dict[str, object]:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.mark.unit
def test_machine_declares_every_phase_of_the_mode_sequences() -> None:
    machine = _contract()["state_machine"]
    assert isinstance(machine, dict)
    assert {s["state_name"] for s in machine["states"]} == _PHASES


@pytest.mark.unit
def test_contract_walker_reports_no_unreachable_dead_or_uncovered_states() -> None:
    report = walk_contracts([_REPO / "src"])
    owned = [w for w in report.workflows if w.workflow_owner == _NODE]
    assert len(owned) == 1
    walk = owned[0]
    assert not walk.unreachable_states
    assert not walk.dead_states
    assert not walk.no_golden_exit_states
    assert not walk.uncovered_transitions
    assert _NODE not in {n.node for n in report.not_armed}


@pytest.mark.unit
def test_terminal_events_are_published_success_and_failure() -> None:
    contract = _contract()
    events = contract["terminal_events"]
    assert isinstance(events, dict)
    assert set(events) == {"success", "failure"}
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    assert events["success"] in event_bus["publish_topics"]
    assert events["failure"] in event_bus["publish_topics"]
    assert events["success"].endswith("build-loop-orchestrator-completed.v1")
    assert events["failure"].endswith("build-loop-failed.v1")
    assert contract["terminal_event"] == events["success"]
