# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared FSM state-coverage regression tests for
node_pr_lifecycle_orchestrator.

OMN-13674 (cluster pr_lifecycle_pipeline, archetype orchestrator) under the full
declared-state-coverage DoD. Pins this ORCHESTRATOR's contract-declared FSM
against the shipped code so a silent contract/handler drift fails here rather
than only at a live runtime boundary:

  * every ``fsm.states`` literal in ``contract.yaml`` has a matching
    ``EnumOrchestratorState`` member (and vice-versa);
  * the initial and terminal states agree between contract and code;
  * every declared command/event topic keeps its literal wire string;
  * the declared terminal_event string is the completed topic the runtime
    publishes.

The behavioural driving of every state/edge over the bus lives in
``test_pr_lifecycle_orchestrator_bus_coverage.py``; this module is the static
contract pin that guards the vocabulary those tests assert against.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    TOPIC_COMPLETED,
    TOPIC_PHASE_TRANSITION,
    TOPIC_PR_LIFECYCLE_START,
    EnumOrchestratorState,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_lifecycle_orchestrator"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text()))


def test_contract_states_match_code_enum() -> None:
    """Every declared fsm state has an EnumOrchestratorState member and vice-versa."""
    declared = set(_load_contract()["fsm"]["states"])
    code = {s.value for s in EnumOrchestratorState}
    assert declared == code, (
        f"contract/code FSM state drift: "
        f"contract-only={sorted(declared - code)} code-only={sorted(code - declared)}"
    )


def test_contract_initial_and_terminal_states() -> None:
    """Initial + terminal state declarations agree with the code enum."""
    fsm = _load_contract()["fsm"]
    assert fsm["initial_state"] == EnumOrchestratorState.IDLE.value
    assert set(fsm["terminal_states"]) == {
        EnumOrchestratorState.COMPLETE.value,
        EnumOrchestratorState.FAILED.value,
    }


def test_contract_transitions_only_reference_declared_states() -> None:
    """Every transition endpoint is a declared FSM state (no dangling edges)."""
    fsm = _load_contract()["fsm"]
    states = set(fsm["states"])
    for transition in fsm["transitions"]:
        assert transition["from"] in states, f"unknown from-state: {transition}"
        assert transition["to"] in states, f"unknown to-state: {transition}"


def test_contract_declares_every_failure_edge_to_failed() -> None:
    """Each non-terminal working state that can error declares a `-> FAILED` edge.

    The bus-coverage suite drives each of these; this pins the contract so a
    dropped failure edge is caught statically."""
    fsm = _load_contract()["fsm"]
    edges = {(t["from"], t["to"]) for t in fsm["transitions"]}
    for from_state in ("INVENTORYING", "TRIAGING", "VERIFYING", "MERGING", "FIXING"):
        assert (from_state, "FAILED") in edges, f"missing {from_state}->FAILED edge"
    assert ("POST_MERGE_TAIL", "FAILED") in edges


def test_contract_topics_keep_literal_wire_strings() -> None:
    """Declared subscribe/publish topics keep the exact wire strings the code uses."""
    event_bus = _load_contract()["event_bus"]
    assert TOPIC_PR_LIFECYCLE_START in event_bus["subscribe_topics"]
    assert TOPIC_PHASE_TRANSITION in event_bus["publish_topics"]
    assert TOPIC_COMPLETED in event_bus["publish_topics"]


def test_contract_terminal_event_is_completed_topic() -> None:
    """The declared terminal_event is the completed topic the runtime publishes."""
    assert _load_contract()["terminal_event"] == TOPIC_COMPLETED
