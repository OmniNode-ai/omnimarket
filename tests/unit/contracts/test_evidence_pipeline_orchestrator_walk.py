# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Walker test for node_evidence_pipeline_orchestrator's declared state machine.

The machine mirrors HandlerEvidencePipelineOrchestrator.handle: collect, extract,
match, write the OCC PR and publish. The handler has no failure branch (a port
exception propagates), so the contract declares one success terminal event and
no failure event or error state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validation.validator_contract_walker import walk_contracts

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_evidence_pipeline_orchestrator"
)
CONTRACT = NODE_DIR / "contract.yaml"
COMPLETED_TOPIC = "onex.evt.omnimarket.evidence-pipeline-completed.v1"
STATES = ["COLLECTING", "EXTRACTING", "MATCHING", "GENERATING_PR", "COMPLETE"]


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_contract_declares_terminal_events() -> None:
    contract = _contract()
    assert contract["terminal_events"] == {"success": COMPLETED_TOPIC}
    assert contract["terminal_event"] == COMPLETED_TOPIC
    publish = contract["event_bus"]["publish_topics"]
    assert COMPLETED_TOPIC in publish


def test_walker_finds_no_defects() -> None:
    summary = walk_contracts([CONTRACT]).summary
    assert summary.walked_contracts == 1
    assert summary.not_armed_contracts == 0
    assert summary.unreachable_states == 0
    assert summary.dead_states == 0
    assert summary.no_golden_exit_states == 0
    assert summary.uncovered_transitions == 0
    assert summary.truncated_workflows == 0
    assert summary.golden_paths == 1
    assert summary.error_paths == 0


def test_golden_path_is_the_handler_call_order() -> None:
    (workflow,) = walk_contracts([CONTRACT]).workflows
    (path,) = workflow.paths
    visited = [path.steps[0].from_state[0]] + [s.to_state[0] for s in path.steps]
    assert visited == STATES


def test_every_off_path_pair_is_rejected() -> None:
    """The handler is linear: each reached non-terminal state accepts exactly its
    own next trigger, so every other (state, trigger) pair is decided as a
    rejection by the absence of a transition."""
    (workflow,) = walk_contracts([CONTRACT]).workflows
    transitions = {
        (t["from_state"], t["trigger"])
        for t in _contract()["state_machine"]["transitions"]
    }
    triggers = {trigger for _, trigger in transitions}
    non_terminal = set(STATES) - {"COMPLETE"}
    expected = {
        (state, trigger)
        for state in non_terminal
        for trigger in triggers
        if (state, trigger) not in transitions
    }
    reported = {(p.state, p.trigger) for p in workflow.illegal_pairs}
    assert reported == expected
    assert len(reported) == 12
