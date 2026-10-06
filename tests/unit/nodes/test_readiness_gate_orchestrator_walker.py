# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-walker findings for node_readiness_gate_orchestrator.

The FSM mirrors HandlerReadinessGateOrchestrator.handle, which keeps no state:
one call unwraps the request, scores a gap report (or takes a readiness result
as is) and publishes the gate. The walker must find every state reachable with
a path to a terminal, and the contract must name its terminal events.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

walker = pytest.importorskip("omnibase_core.validation.validator_contract_walker")

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_readiness_gate_orchestrator"
)


def _workflow() -> object:
    report = walker.walk_contracts([NODE_DIR])
    assert not report.not_armed
    assert len(report.workflows) == 1
    return report.workflows[0]


def test_walker_finds_no_unreachable_dead_or_uncovered_states() -> None:
    wf = _workflow()
    assert wf.unreachable_states == ()
    assert wf.dead_states == ()
    assert wf.no_golden_exit_states == ()
    assert wf.uncovered_transitions == ()


def test_walker_illegal_pairs_are_only_out_of_order_triggers() -> None:
    # The handler is stateless and runs COLLECTING -> ... -> terminal inside one
    # call, so an out-of-order trigger cannot be delivered; each pair below is
    # rejected by having no transition. Nothing else may appear undecided.
    wf = _workflow()
    pairs = {(p.state, p.trigger) for p in wf.illegal_pairs}
    assert pairs == {
        ("COLLECTING", "gap_report_scored"),
        ("COLLECTING", "gate_published_ready"),
        ("COLLECTING", "gate_published_blocked"),
        ("SCORING", "gap_report_received"),
        ("SCORING", "readiness_result_received"),
        ("SCORING", "gate_published_ready"),
        ("SCORING", "gate_published_blocked"),
        ("REPORTING", "gap_report_received"),
        ("REPORTING", "readiness_result_received"),
        ("REPORTING", "gap_report_scored"),
    }


def test_contract_declares_terminal_events_published_by_the_node() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    terminals = contract["terminal_events"]
    assert terminals == {
        "success": "onex.evt.omnimarket.readiness-gate-completed.v1",
        "failure": "onex.evt.omnimarket.readiness-gate-blocked.v1",
    }
    assert set(terminals.values()) <= set(contract["event_bus"]["publish_topics"])
    assert contract["terminal_event"] == terminals["success"]


def test_contract_states_match_the_handler_steps() -> None:
    fsm = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())["state_machine"]
    assert fsm["initial_state"] == "COLLECTING"
    assert fsm["terminal_states"] == ["COMPLETE", "BLOCKED"]
    assert [s["state_name"] for s in fsm["states"]] == [
        "COLLECTING",
        "SCORING",
        "REPORTING",
        "COMPLETE",
        "BLOCKED",
    ]
