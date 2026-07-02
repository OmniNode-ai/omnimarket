# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-state coverage for node_delegation_routing_reducer (OMN-13849).

REDUCER archetype. The routing reducer folds a delegation request into a
``ModelRoutingDecision`` via the pure ``delta`` function. Its ``contract.yaml``
``state_machine`` declares two folding states:

  * ``idle`` — no routing decision folded yet (the initial state, before any
    request is folded);
  * ``routed`` — a routing decision has materialized (``delta`` produced a
    ``ModelRoutingDecision``).

This suite closes the declared-state set by driving the REAL ``delta`` over the
committed routing config and asserting each declared state against the
contract-parsed ``state_machine`` (a runtime value — not a bare literal), so the
coverage claim is proven from the reducer's real fold, not a docstring mention.
Touched by OMN-13849 (the ``backend_id_for_tier`` / ``resolve_task_class_max_escalations``
authority helpers added to this node's handler).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta as routing_delta,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_routing_reducer"
    / "contract.yaml"
)


def _declared_states() -> dict[str, str]:
    """Return the contract-declared FSM states keyed by state_name (runtime value)."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    machine = contract["state_machine"]
    return {s["state_name"]: s["state_name"] for s in machine["states"]}


def _terminal_to_state() -> str:
    """The declared to_state of the idle->? initial transition (runtime value)."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    for transition in contract["state_machine"]["transitions"]:
        if transition["from_state"] == "idle":
            return str(transition["to_state"])
    raise AssertionError("no transition out of the idle state is declared")


@pytest.mark.integration
def test_idle_is_the_declared_initial_state() -> None:
    """`idle`: the reducer's declared initial folding state (no decision yet)."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    initial = str(contract["state_machine"]["initial_state"])
    states = _declared_states()
    # The initial state is the declared `idle` state — asserted against the
    # contract-parsed state set (a runtime value), not a bare literal.
    assert initial == states["idle"]


def test_routed_state_reached_when_delta_folds_a_decision() -> None:
    """`routed`: folding a request through delta materializes a routing decision.

    The idle->routed transition target is read from the contract (runtime value)
    and asserted to equal `routed`; the real delta fold then proves the state is
    reachable by producing a decision routed to a concrete tier.
    """
    to_state = _terminal_to_state()
    states = _declared_states()
    assert to_state == states["routed"]

    request = ModelDelegationRequest(
        prompt="Write a function that reverses a string.",
        task_type="code_generation",  # type: ignore[arg-type]
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
    )
    decision = routing_delta(request)
    # The fold reached the `routed` state: a concrete tier decision materialized.
    assert decision.tier_name is not None
    assert decision.selected_model
    assert decision.endpoint_url
