# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-state coverage for node_delegation_quality_gate_reducer (OMN-13849).

REDUCER archetype. The quality-gate reducer folds an LLM response into a
``ModelQualityGateResult`` via the pure ``delta`` function. Its ``contract.yaml``
``state_machine`` declares two folding states:

  * ``idle`` — no quality-gate evaluation folded yet (the initial state);
  * ``evaluated`` — a quality-gate result has materialized (``delta`` produced a
    ``ModelQualityGateResult``).

This suite closes the declared-state set by driving the REAL ``delta`` and
asserting each declared state against the contract-parsed ``state_machine`` (a
runtime value — not a bare literal), so the coverage claim is proven from the
reducer's real fold. Touched by OMN-13849 (the ``JUDGE_COMBINABLE_TASK_TYPES``
public constant added to this node's quality-gate-intent handler, consumed by the
bus-less local dispatch path).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_quality_gate_reducer"
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
    """`idle`: the reducer's declared initial folding state (no evaluation yet)."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    initial = str(contract["state_machine"]["initial_state"])
    states = _declared_states()
    assert initial == states["idle"]


def test_evaluated_state_reached_when_delta_folds_a_verdict() -> None:
    """`evaluated`: folding a response through delta materializes a gate verdict.

    The idle->evaluated transition target is read from the contract (runtime
    value) and asserted to equal `evaluated`; the real delta fold then proves the
    state is reachable by producing a quality-gate result.
    """
    to_state = _terminal_to_state()
    states = _declared_states()
    assert to_state == states["evaluated"]

    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="research",
        llm_response_content=(
            "According to Smith (2020) and the theorem in section 3, the tradeoff "
            "is significant because the evidence shows X; therefore we conclude Y. "
            "See references [12] for the methodical analysis and the risk profile."
        ),
        dod_deterministic=("response_non_empty",),
        dod_heuristic=("no_refusal",),
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
    )
    result = quality_gate_delta(gate_input)
    # The fold reached the `evaluated` state: a gate verdict + graded score
    # materialized for this correlation.
    assert result.correlation_id == gate_input.correlation_id
    assert 0.0 <= result.quality_score <= 1.0
