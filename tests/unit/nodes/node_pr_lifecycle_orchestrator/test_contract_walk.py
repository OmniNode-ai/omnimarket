# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR lifecycle orchestrator's contract is walkable and declares its terminals.

Walks the real ``contract.yaml`` with omnibase_core's contract walker
(validation/validator_contract_walker.py) composed with the state reducer the
contract links to, and asserts the machine the handler drives has no
unreachable, dead, exitless or uncovered part. The declared states are also
held to the handler's ``EnumOrchestratorState``, so the contract cannot drift
from the one method that changes phase. The terminal events are held to the
contract's own publish list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.models.validation.model_contract_walk_workflow import (
    ModelContractWalkWorkflow,
)
from omnibase_core.validation.validator_contract_walker import walk_contracts

from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    EnumOrchestratorState,
)

pytestmark = pytest.mark.unit

_NODE = "node_pr_lifecycle_orchestrator"
_NODE_DIR = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes" / _NODE
_CONTRACT = _NODE_DIR / "contract.yaml"


def _contract() -> dict[str, object]:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _workflow() -> ModelContractWalkWorkflow:
    report = walk_contracts([_NODE_DIR.parent.parent.parent])
    owned = [w for w in report.workflows if w.workflow_owner == _NODE]
    assert len(owned) == 1, "the orchestrator must be walked as one workflow"
    return owned[0]


def test_walker_reports_no_unreachable_dead_or_uncovered_part() -> None:
    workflow = _workflow()
    assert workflow.unreachable_states == ()
    assert workflow.dead_states == ()
    assert workflow.no_golden_exit_states == ()
    assert workflow.uncovered_transitions == ()
    assert not workflow.truncated


def test_walker_finds_a_golden_path_and_an_error_edge() -> None:
    workflow = _workflow()
    kinds = {str(getattr(p.kind, "value", p.kind)).lower() for p in workflow.paths}
    assert "golden" in kinds
    assert workflow.error_edges


def test_machine_states_are_the_handler_states() -> None:
    machine = _contract()["state_machine"]
    assert isinstance(machine, dict)
    declared = {s["state_name"] for s in machine["states"]}
    assert declared == {s.value for s in EnumOrchestratorState}
    assert machine["initial_state"] == EnumOrchestratorState.IDLE.value
    assert set(machine["terminal_states"]) == {
        EnumOrchestratorState.COMPLETE.value,
        EnumOrchestratorState.FAILED.value,
    }


def test_terminal_events_are_declared_and_published() -> None:
    contract = _contract()
    terminal = contract["terminal_events"]
    assert isinstance(terminal, dict)
    assert set(terminal) == {"success", "failure"}
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    published = set(event_bus["publish_topics"])
    assert terminal["success"] in published
    assert terminal["failure"] in published
    assert terminal["success"] == contract["terminal_event"]
