# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Contract-bound FSM tests for node_delegation_orchestrator [OMN-13474].

W2 of the OMN-13471 delegation decomposition binds the handler's FSM transition
guard to the node's ``contract.yaml`` ``fsm`` block via the typed,
executor-bound ``ModelFSMSubcontract`` (the OMN-12835 typed contract-side
workflow surface). The hardcoded ``_VALID_TRANSITIONS`` Python literal is gone;
the runtime guard table is now a projection of the contract-derived typed FSM.

These tests prove:
  * the runtime guard table equals the table derived from the declared contract
    edges (no parallel hand-maintained dict);
  * the typed ``ModelFSMSubcontract`` is real (initial/terminal/transitions match
    the contract) and is consumable by the canonical core FSM executor
    (``omnibase_core.utils.util_fsm_executor.execute_transition``) — i.e. the
    declared table is genuinely executor-bound, not a dead schema field;
  * every declared contract edge is drivable through the core executor against
    the typed FSM with the same target state the imperative guard reaches.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.utils.util_fsm_executor import execute_transition

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _FSM_SUBCONTRACT,
    _VALID_TRANSITIONS,
    _build_valid_transitions,
    _load_fsm_subcontract,
)

_CONTRACT_PATH = Path("src/omnimarket/nodes/node_delegation_orchestrator/contract.yaml")


def _contract_fsm_block() -> dict[str, object]:
    with _CONTRACT_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)["fsm"]


@pytest.mark.unit
class TestContractBoundFsm:
    """The contract fsm block is the runtime execution authority (OMN-13474)."""

    def test_fsm_subcontract_is_typed_and_matches_contract(self) -> None:
        """The module-level typed FSM reflects the contract verbatim."""
        block = _contract_fsm_block()
        assert isinstance(_FSM_SUBCONTRACT, ModelFSMSubcontract)
        assert _FSM_SUBCONTRACT.initial_state == block["initial_state"]
        assert sorted(_FSM_SUBCONTRACT.terminal_states) == sorted(
            block["terminal_states"]
        )
        assert {s.state_name for s in _FSM_SUBCONTRACT.states} == set(block["states"])
        assert len(_FSM_SUBCONTRACT.transitions) == len(block["transitions"])

    def test_valid_transitions_is_contract_derived_projection(self) -> None:
        """``_VALID_TRANSITIONS`` is the projection of the typed contract FSM.

        Re-loading the contract from disk and re-projecting must reproduce the
        in-memory guard table exactly — proving it is not a parallel literal.
        """
        reloaded = _build_valid_transitions(_load_fsm_subcontract())
        assert reloaded == _VALID_TRANSITIONS

    def test_guard_table_equals_declared_contract_edges(self) -> None:
        """Every guard edge is a declared contract edge and vice versa."""
        block = _contract_fsm_block()
        contract_edges = {(t["from"], t["to"]) for t in block["transitions"]}
        guard_edges = {
            (frm.value, to.value)
            for frm, targets in _VALID_TRANSITIONS.items()
            for to in targets
        }
        assert guard_edges == contract_edges

    async def test_every_contract_edge_drivable_by_core_executor(self) -> None:
        """Each declared edge transitions to the same target via the core executor.

        Proves the typed FSM is executor-bound: the canonical
        ``execute_transition`` resolves each (from_state, trigger) declared in
        the contract to the contract's declared ``to`` state — the same target
        the imperative ``_transition`` guard would reach.
        """
        block = _contract_fsm_block()
        for entry in block["transitions"]:
            result = await execute_transition(
                _FSM_SUBCONTRACT,
                current_state=entry["from"],
                trigger=entry["trigger"],
                context={},
            )
            assert result.success, (
                f"core executor rejected declared edge "
                f"{entry['from']} -> {entry['to']} on trigger {entry['trigger']!r}: "
                f"{result.error}"
            )
            assert result.new_state == entry["to"]
            # The guard table the imperative handler enforces must also permit it.
            assert (
                EnumDelegationState(entry["to"])
                in _VALID_TRANSITIONS[EnumDelegationState(entry["from"])]
            )

    def test_unknown_state_would_be_unmappable(self) -> None:
        """An undeclared state has no EnumDelegationState member.

        Backs the loader's fail-fast guard (Operating Rule #8): a drifted
        contract that introduces a state with no enum member is rejected rather
        than silently dropping the unmapped edge. Every real declared state maps;
        a synthetic ``BOGUS_STATE`` does not.
        """
        enum_names = {s.value for s in EnumDelegationState}
        block = _contract_fsm_block()
        assert set(block["states"]) <= enum_names
        assert "BOGUS_STATE" not in enum_names
