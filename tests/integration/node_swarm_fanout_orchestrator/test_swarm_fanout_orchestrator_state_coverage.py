# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared FSM state-coverage regression tests for
node_swarm_fanout_orchestrator.

Pins this ORCHESTRATOR's contract-declared FSM against the shipped code so a
silent contract/handler drift fails here rather than only at a live runtime
boundary:

  * every ``fsm.states`` literal in ``contract.yaml`` has a matching
    ``EnumFanoutFsmState`` member (and vice-versa);
  * the initial state agrees between contract and code;
  * every transition endpoint is a declared state (no dangling edges);
  * every declared publish/subscribe topic keeps its literal wire string;
  * the declared terminal_event string is the completed topic the runtime
    publishes.

OMN-14586: added as a side effect of relocating ``ModelSwarmFanoutResult``
into ``omnimarket.events`` — the state-coverage gate (OMN-13781) treats any
touch inside this node's package as "directly modified" and promotes its
pre-existing, baselined PLANNING/COLLECTING coverage gap to a hard fail. This
module closes that gap for real (contract/code cross-check, not a decorative
string mention) rather than expanding the ticket's model-relocation scope
into unrelated FSM-behavior testing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from omnimarket.nodes.node_swarm_fanout_orchestrator.models.enums import (
    EnumFanoutFsmState,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_swarm_fanout_orchestrator"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text()))


def test_contract_states_match_code_enum() -> None:
    """Every declared fsm state has an EnumFanoutFsmState member and vice-versa."""
    declared = set(_load_contract()["fsm"]["states"])
    code = {s.value for s in EnumFanoutFsmState}
    assert declared == code, (
        f"contract/code FSM state drift: "
        f"contract-only={sorted(declared - code)} code-only={sorted(code - declared)}"
    )


def test_contract_initial_state() -> None:
    """Initial state declaration agrees with the code enum."""
    fsm = _load_contract()["fsm"]
    assert fsm["initial"] == EnumFanoutFsmState.PLANNING.value


def test_contract_transitions_only_reference_declared_states() -> None:
    """Every transition endpoint is a declared FSM state (no dangling edges)."""
    fsm = _load_contract()["fsm"]
    states = set(fsm["states"])
    for transition in fsm["transitions"]:
        assert transition["from"] in states, f"unknown from-state: {transition}"
        assert transition["to"] in states, f"unknown to-state: {transition}"


def test_contract_terminal_states_have_no_outgoing_transitions() -> None:
    """COMPLETED/FAILED are terminal: no transition declares them as `from`."""
    fsm = _load_contract()["fsm"]
    from_states = {t["from"] for t in fsm["transitions"]}
    assert EnumFanoutFsmState.COMPLETED.value not in from_states
    assert EnumFanoutFsmState.FAILED.value not in from_states


def test_contract_collecting_state_transitions() -> None:
    """COLLECTING is reached from DISPATCHING and forks to COMPLETED or FAILED.

    Pins the specific edges through the wave-collection state so a dropped
    transition (e.g. losing the fork to FAILED) fails here rather than only
    during a live wave-collection incident. Compares against
    ``EnumFanoutFsmState`` members directly rather than the contract's
    ``trigger:`` labels — no behavior here depends on that key's name.

    Historical note (OMN-14596): this transitions block used to declare its
    trigger label under a bare ``on:`` key, which PyYAML's default (YAML 1.1)
    resolver parses as the boolean ``True`` rather than the string ``"on"``
    (the "Norway problem" — ``transition["on"]`` raised ``KeyError``). The
    key was renamed to ``trigger:`` to remove the landmine at the source;
    this test still avoids depending on the trigger key entirely.
    """
    fsm = _load_contract()["fsm"]
    edges = {(t["from"], t["to"]) for t in fsm["transitions"]}
    dispatching = EnumFanoutFsmState.DISPATCHING.value
    collecting = EnumFanoutFsmState.COLLECTING.value
    completed = EnumFanoutFsmState.COMPLETED.value
    failed = EnumFanoutFsmState.FAILED.value

    assert (dispatching, collecting) in edges
    assert (collecting, completed) in edges
    assert (collecting, failed) in edges


def test_contract_transitions_trigger_key_is_a_real_string() -> None:
    """OMN-14596 regression: the trigger key must survive ``yaml.safe_load``
    as the string ``"trigger"``, not be swallowed into the Python bool
    ``True`` key by PyYAML's YAML 1.1 "Norway problem" resolver.

    Before the fix, this exact contract declared the trigger label under a
    bare ``on:`` key. ``yaml.safe_load("on: commands_built")`` resolves to
    ``{True: "commands_built"}`` — the string key ``"on"`` never exists in
    the loaded dict, and any code accessing ``transition["on"]`` raises
    ``KeyError``. This test drives the real PyYAML loader against the real
    on-disk contract (not a synthetic snippet) and asserts both that the
    boolean-key landmine is gone and that the intended string key resolves
    to the expected trigger label for every declared transition.
    """
    fsm = _load_contract()["fsm"]
    transitions = fsm["transitions"]
    assert transitions, "contract.yaml fsm.transitions must not be empty"

    expected_triggers = {
        ("PLANNING", "DISPATCHING"): "commands_built",
        ("DISPATCHING", "COLLECTING"): "all_commands_published",
        ("COLLECTING", "COMPLETED"): "all_subtasks_terminal",
        ("COLLECTING", "FAILED"): "fatal_error",
    }

    for transition in transitions:
        # The Norway-problem landmine: PyYAML's safe_load resolver would
        # fold a bare `on:`/`off:`/`yes:`/`no:` key to a Python bool. Assert
        # no boolean key is present in any transition dict.
        assert True not in transition, (
            f"transition dict contains a boolean-True key (Norway problem "
            f"regression): {transition}"
        )
        assert False not in transition, (
            f"transition dict contains a boolean-False key (Norway problem "
            f"regression): {transition}"
        )

        edge = (transition["from"], transition["to"])
        assert "trigger" in transition, (
            f"transition missing string 'trigger' key: {transition}"
        )
        trigger = transition["trigger"]
        assert isinstance(trigger, str), (
            f"trigger value must be a string, got {type(trigger)}: {transition}"
        )
        assert trigger == expected_triggers[edge], (
            f"unexpected trigger label for edge {edge}: {trigger!r} != "
            f"{expected_triggers[edge]!r}"
        )


def test_contract_topics_keep_literal_wire_strings() -> None:
    """Declared subscribe/publish topics match the contract's own event vocabulary."""
    event_bus = _load_contract()["event_bus"]
    assert "onex.cmd.omnimarket.swarm-fanout.v1" in event_bus["subscribe_topics"]
    assert (
        "onex.evt.omnimarket.swarm-fanout-completed.v1" in event_bus["publish_topics"]
    )


def test_contract_terminal_event_is_completed_topic() -> None:
    """The declared terminal_event is the completed topic the runtime publishes."""
    contract = _load_contract()
    assert contract["terminal_event"] == "onex.evt.omnimarket.swarm-fanout-completed.v1"
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
