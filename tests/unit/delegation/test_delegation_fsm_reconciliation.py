# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""FSM reconciliation tests for node_delegation_orchestrator [OMN-13473].

The delegation orchestrator handler re-implements its FSM imperatively via
``HandlerDelegationWorkflow._transition`` calls guarded by the
``_VALID_TRANSITIONS`` table. The canonical FSM is declared in
``contract.yaml`` under ``fsm.transitions``. These tests make the contract the
single source of truth: every edge the handler can drive must be a declared
contract transition (subset relation), and the documented 1:1 mapping of each
``_transition`` call site to its contract entry must stay in sync.

DoD (OMN-13473):
  * Documented 1:1 mapping of handler-driven transitions to contract entries
    (the ``HANDLER_TRANSITION_CALLSITES`` fixture below).
  * Every handler transition maps to a declared contract FSM entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _VALID_TRANSITIONS,
)

_CONTRACT_PATH = Path("src/omnimarket/nodes/node_delegation_orchestrator/contract.yaml")


# ---------------------------------------------------------------------------
# Documented 1:1 mapping: every ``self._transition(workflow, <to>)`` call site
# in handler_delegation_workflow.py, with the resolved ``from`` state and the
# handler method that drives it. ``from`` is resolved from the call-site guard
# (e.g. ``if workflow.state != ROUTED: return`` -> from == ROUTED) or the
# immediately preceding transition. Lifecycle COMPLETED/FAILED can fire from
# either EXECUTING (post-PROGRESS) or ROUTED (terminal lifecycle before any
# PROGRESS event), so both origins are enumerated.
#
# (from_state, to_state, handler_method)
# ---------------------------------------------------------------------------
HANDLER_TRANSITION_CALLSITES: tuple[
    tuple[EnumDelegationState, EnumDelegationState, str], ...
] = (
    # handle_invocation_command — guard: state == RECEIVED
    (
        EnumDelegationState.RECEIVED,
        EnumDelegationState.ROUTED,
        "handle_invocation_command",
    ),
    # handle_routing_decision — branch: if state == RECEIVED
    (
        EnumDelegationState.RECEIVED,
        EnumDelegationState.ROUTED,
        "handle_routing_decision",
    ),
    # handle_inference_response — guard: state == ROUTED; retryable infra-error escalation
    (
        EnumDelegationState.ROUTED,
        EnumDelegationState.ESCALATING,
        "handle_inference_response",
    ),
    # handle_inference_response — ESCALATING -> ROUTED re-route to next tier
    (
        EnumDelegationState.ESCALATING,
        EnumDelegationState.ROUTED,
        "handle_inference_response",
    ),
    # handle_inference_response — terminal failure, no escalation possible
    (
        EnumDelegationState.ROUTED,
        EnumDelegationState.FAILED,
        "handle_inference_response",
    ),
    # handle_inference_response (legacy, no compliance loop) — accept response
    (
        EnumDelegationState.ROUTED,
        EnumDelegationState.INFERENCE_COMPLETED,
        "handle_inference_response",
    ),
    # _evaluate_compliance (compliance loop) — accept / budget-abort
    (
        EnumDelegationState.ROUTED,
        EnumDelegationState.INFERENCE_COMPLETED,
        "_evaluate_compliance",
    ),
    # _evaluate_compliance — non-compliant + budget CONTINUE -> ROUTED self-loop
    (EnumDelegationState.ROUTED, EnumDelegationState.ROUTED, "_evaluate_compliance"),
    # handle_gate_result — guard: state == INFERENCE_COMPLETED
    (
        EnumDelegationState.INFERENCE_COMPLETED,
        EnumDelegationState.GATE_EVALUATED,
        "handle_gate_result",
    ),
    # handle_gate_result — gate failed terminally
    (
        EnumDelegationState.GATE_EVALUATED,
        EnumDelegationState.FAILED,
        "handle_gate_result",
    ),
    # handle_gate_result — gate passed
    (
        EnumDelegationState.GATE_EVALUATED,
        EnumDelegationState.COMPLETED,
        "handle_gate_result",
    ),
    # handle_gate_result — gate failed with fallback + budget -> escalate
    (
        EnumDelegationState.GATE_EVALUATED,
        EnumDelegationState.ESCALATING,
        "handle_gate_result",
    ),
    # handle_gate_result — ESCALATING -> ROUTED re-route with tier override
    (EnumDelegationState.ESCALATING, EnumDelegationState.ROUTED, "handle_gate_result"),
    # handle_gate_result — terminal failure on exhausted escalation
    (
        EnumDelegationState.GATE_EVALUATED,
        EnumDelegationState.FAILED,
        "handle_gate_result",
    ),
    # handle_agent_task_lifecycle — PROGRESS/SUBMITTED/ACCEPTED/ARTIFACT from ROUTED
    (
        EnumDelegationState.ROUTED,
        EnumDelegationState.EXECUTING,
        "handle_agent_task_lifecycle",
    ),
    # handle_agent_task_lifecycle — COMPLETED from EXECUTING (normal path)
    (
        EnumDelegationState.EXECUTING,
        EnumDelegationState.COMPLETED,
        "handle_agent_task_lifecycle",
    ),
    # handle_agent_task_lifecycle — FAILED from EXECUTING (normal path)
    (
        EnumDelegationState.EXECUTING,
        EnumDelegationState.FAILED,
        "handle_agent_task_lifecycle",
    ),
    # handle_agent_task_lifecycle — COMPLETED before any PROGRESS (still ROUTED)
    (
        EnumDelegationState.ROUTED,
        EnumDelegationState.COMPLETED,
        "handle_agent_task_lifecycle",
    ),
    # handle_agent_task_lifecycle — FAILED before any PROGRESS (still ROUTED)
    (
        EnumDelegationState.ROUTED,
        EnumDelegationState.FAILED,
        "handle_agent_task_lifecycle",
    ),
)


def _load_contract_transitions() -> set[tuple[str, str]]:
    """Return the declared (from, to) FSM edges from contract.yaml."""
    with _CONTRACT_PATH.open() as f:
        data = yaml.safe_load(f)
    transitions = data["fsm"]["transitions"]
    return {(t["from"], t["to"]) for t in transitions}


def _valid_transition_edges() -> set[tuple[str, str]]:
    """Return the (from, to) edges encoded in the handler _VALID_TRANSITIONS."""
    return {
        (from_state.value, to_state.value)
        for from_state, targets in _VALID_TRANSITIONS.items()
        for to_state in targets
    }


@pytest.mark.unit
class TestFsmReconciliation:
    """Contract FSM is the single source of truth for handler transitions."""

    def test_handler_valid_transitions_subset_of_contract(self) -> None:
        """Every handler _VALID_TRANSITIONS edge is a declared contract edge."""
        contract_edges = _load_contract_transitions()
        handler_edges = _valid_transition_edges()
        undeclared = handler_edges - contract_edges
        assert not undeclared, (
            "Handler _VALID_TRANSITIONS contains edges not declared in "
            f"contract.yaml fsm.transitions: {sorted(undeclared)}. Add them to "
            "the contract (single source of truth) or remove them from the "
            "handler."
        )

    @pytest.mark.parametrize(
        ("from_state", "to_state", "handler_method"),
        HANDLER_TRANSITION_CALLSITES,
    )
    def test_callsite_transition_is_declared_in_contract(
        self,
        from_state: EnumDelegationState,
        to_state: EnumDelegationState,
        handler_method: str,
    ) -> None:
        """Each documented _transition call site maps to a contract entry."""
        contract_edges = _load_contract_transitions()
        edge = (from_state.value, to_state.value)
        assert edge in contract_edges, (
            f"{handler_method} drives {from_state.value} -> {to_state.value} "
            "but that transition is not declared in contract.yaml fsm.transitions."
        )

    def test_callsite_transition_is_valid_in_handler_table(
        self,
    ) -> None:
        """Each documented call-site edge is permitted by _VALID_TRANSITIONS.

        Guards against the mapping fixture drifting from the actual imperative
        guard table the handler enforces at runtime.
        """
        handler_edges = _valid_transition_edges()
        for from_state, to_state, handler_method in HANDLER_TRANSITION_CALLSITES:
            edge = (from_state.value, to_state.value)
            assert edge in handler_edges, (
                f"{handler_method} maps {edge} but _VALID_TRANSITIONS does not "
                "permit it; the imperative guard would raise "
                "InvalidStateTransitionError at runtime."
            )

    def test_documented_callsites_cover_all_handler_edges(self) -> None:
        """The 1:1 mapping fixture covers every _VALID_TRANSITIONS edge.

        Ensures no handler-permitted transition is left undocumented.
        """
        handler_edges = _valid_transition_edges()
        documented_edges = {
            (f.value, t.value) for f, t, _ in HANDLER_TRANSITION_CALLSITES
        }
        missing = handler_edges - documented_edges
        assert not missing, (
            "Handler _VALID_TRANSITIONS edges with no documented call site in "
            f"HANDLER_TRANSITION_CALLSITES: {sorted(missing)}."
        )

    def test_contract_has_no_unreachable_transitions(self) -> None:
        """Every declared contract edge is reachable from the handler table.

        Keeps the contract from accumulating dead FSM edges that the handler
        never drives.
        """
        contract_edges = _load_contract_transitions()
        handler_edges = _valid_transition_edges()
        unreachable = contract_edges - handler_edges
        assert not unreachable, (
            "contract.yaml declares FSM edges the handler _VALID_TRANSITIONS "
            f"never permits (dead transitions): {sorted(unreachable)}."
        )
