# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-state-coverage gate fix (OMN-13781) for node_canary_score_reducer.

Pre-existing, baselined gap (``scripts/validation/state_coverage_baseline.txt``
line 64: ``node_canary_score_reducer scored``) surfaced FAIL by this ticket's
own new test files under ``tests/unit/nodes/node_canary_score_reducer/`` --
the gate promotes a baselined WARN to FAIL the moment a node is directly
touched by the diff (``--strict``), and this is now the first
``tests/unit/nodes/node_canary_score_reducer/`` directory to exist. Fixed
here rather than left red or worked around, per this repo's no-pre-existing-
excuse policy, with a real assertion against the declared FSM -- not a bare
string mention, which the gate's AST-level check would reject as vacuous
(see ``scripts/validate_state_coverage.py::_is_vacuous_compare``).

This does not touch the node's runtime behavior; it locks the already-shipped
``state_machine`` block of ``contract.yaml`` (OMN-13734) against silent drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONTRACT = _REPO_ROOT / "src/omnimarket/nodes/node_canary_score_reducer/contract.yaml"


def _load_states_by_name() -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")) or {}
    state_machine = raw.get("state_machine")
    assert isinstance(state_machine, dict), f"{_CONTRACT} missing state_machine block"
    states = state_machine.get("states")
    assert isinstance(states, list), f"{_CONTRACT} state_machine.states must be a list"
    assert states, f"{_CONTRACT} state_machine.states must be non-empty"
    return {entry["state_name"]: entry for entry in states}


def test_idle_is_the_declared_initial_state() -> None:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")) or {}
    assert raw["state_machine"]["initial_state"] == "idle"


def test_scored_state_is_declared_with_its_documented_description() -> None:
    """Real (non-vacuous) coverage of the 'scored' FSM state: compares the
    live-parsed contract value against the expected literal, not a bare
    string mention -- the described materialization contract this node's
    projection writer (and this ticket's capability_scores UUID conversion)
    depends on staying true."""
    states = _load_states_by_name()
    scored = states.get("scored")
    assert scored is not None, "contract.yaml no longer declares the 'scored' state"
    assert scored["description"] == "Capability scores projection materialized."
    assert scored["required_data"] == []


def test_idle_to_scored_transition_is_triggered_by_adr_canary_completed() -> None:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")) or {}
    transitions = raw["state_machine"]["transitions"]
    idle_to_scored = [
        t
        for t in transitions
        if t["from_state"] == "idle" and t["to_state"] == "scored"
    ]
    assert len(idle_to_scored) == 1, (
        f"expected exactly one idle->scored transition, found {idle_to_scored}"
    )
    assert idle_to_scored[0]["trigger"] == "adr_canary_completed"


def test_scored_has_no_outgoing_transition_that_leaves_scored() -> None:
    """scored is a stable materialized state: every transition FROM scored
    stays at scored (repeated folds / re-observation), matching
    ``terminal_states: []`` -- the FSM never declares scored as terminal
    while also never routing back to idle."""
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")) or {}
    transitions = raw["state_machine"]["transitions"]
    from_scored = [t for t in transitions if t["from_state"] == "scored"]
    assert from_scored, "expected at least one transition declared from 'scored'"
    destinations = {t["to_state"] for t in from_scored}
    assert destinations == {"scored"}
