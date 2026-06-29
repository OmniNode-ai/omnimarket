# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""FSM state_machine backfill tests - OMN-13734.

DoD:
  - All 15 contracts (14 reducers + redeploy FSM reducer) carry a
    machine-readable state_machine: block.
  - ModelContractReducer.state_machine is non-None for each when loaded.
  - validate_fsm_contract (util_fsm_executor.py:273) passes on all 15 -
    no unreachable or dangling states.
  - Negative case: transition referencing an undeclared state fails
    validate_fsm_contract.

TDD: tests were written BEFORE the state_machine: blocks were added to the
contracts; they fail until the implementation is in place.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_state_definition import (
    ModelFSMStateDefinition,
)
from omnibase_core.models.contracts.subcontracts.model_fsm_state_transition import (
    ModelFSMStateTransition,
)
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.models.primitives.model_semver import ModelSemVer
from omnibase_core.utils.util_fsm_executor import validate_fsm_contract

pytestmark = pytest.mark.unit

_NODES_ROOT = Path("src/omnimarket/nodes")

# 14 reducer contracts that need state_machine backfill (OMN-13734).
_REDUCER_CONTRACTS: tuple[str, ...] = (
    "node_ab_compare_reducer",
    "node_canary_score_reducer",
    "node_contract_reducer",
    "node_delegation_quality_gate_reducer",
    "node_delegation_routing_feedback_reducer",
    "node_delegation_routing_reducer",
    "node_deployment_evidence_reducer",
    "node_evidence_dashboard_reducer",
    "node_knowledge_context_assembler_reducer",
    "node_ledger_state_reducer",
    "node_merge_sweep_state_reducer",
    "node_navigation_history_reducer",
    "node_session_phase_reducer",
    "node_swarm_subtask_state_reducer",
)

# The current dev branch represents redeploy's FSM as a pure reducer node.
_WORKFLOW_CONTRACTS: tuple[str, ...] = ("node_redeploy_fsm_reducer",)

_ALL_CONTRACTS: tuple[str, ...] = _REDUCER_CONTRACTS + _WORKFLOW_CONTRACTS

_FSM_VERSION = ModelSemVer(major=1, minor=0, patch=0)


def _load_contract_yaml(node_name: str) -> dict[str, Any]:
    contract_path = _NODES_ROOT / node_name / "contract.yaml"
    with contract_path.open() as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), f"{contract_path} must load as a YAML mapping"
    return data


def _build_fsm_from_yaml(sm_data: dict[str, Any]) -> ModelFSMSubcontract:
    """Normalise a simplified YAML state_machine block into ModelFSMSubcontract.

    The YAML contracts use the simple format (no version/state_type/
    transition_name); this helper fills in the required Pydantic fields so
    validate_fsm_contract can be called directly.
    """
    terminal_names: set[str] = set(sm_data.get("terminal_states", []))

    states: list[ModelFSMStateDefinition] = []
    for s in sm_data["states"]:
        name = s["state_name"]
        is_term = s.get("is_terminal", False) or name in terminal_names
        states.append(
            ModelFSMStateDefinition(
                version=_FSM_VERSION,
                state_name=name,
                state_type="terminal" if is_term else "operational",
                description=s.get("description", name),
                is_terminal=is_term,
                is_recoverable=not is_term,
                entry_actions=list(s.get("entry_actions", [])),
                exit_actions=list(s.get("exit_actions", [])),
                required_data=list(s.get("required_data", [])),
            )
        )

    transitions: list[ModelFSMStateTransition] = []
    for idx, t in enumerate(sm_data["transitions"]):
        transitions.append(
            ModelFSMStateTransition(
                version=_FSM_VERSION,
                transition_name=f"t{idx}_{t['from_state']}_to_{t['to_state']}",
                from_state=t["from_state"],
                to_state=t["to_state"],
                trigger=t["trigger"],
            )
        )

    return ModelFSMSubcontract(
        version=_FSM_VERSION,
        state_machine_name=sm_data["state_machine_name"],
        state_machine_version=_FSM_VERSION,
        description=sm_data.get("description", sm_data["state_machine_name"]),
        states=states,
        initial_state=sm_data["initial_state"],
        terminal_states=list(terminal_names),
        transitions=transitions,
        persistence_enabled=bool(sm_data.get("persistence_enabled", False)),
    )


# ---------------------------------------------------------------------------
# Positive tests - state_machine block must be present and valid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node_name", _ALL_CONTRACTS)
def test_contract_has_state_machine_block(node_name: str) -> None:
    """Each contract must carry a non-None state_machine: YAML block."""
    contract = _load_contract_yaml(node_name)
    sm = contract.get("state_machine")
    assert sm is not None, (
        f"{node_name}/contract.yaml is missing the state_machine: block "
        "(OMN-13734 backfill not yet applied)"
    )
    assert isinstance(sm, dict), (
        f"{node_name}/contract.yaml state_machine must be a YAML mapping"
    )
    assert "state_machine_name" in sm, (
        f"{node_name} state_machine is missing state_machine_name"
    )
    assert "initial_state" in sm, f"{node_name} state_machine is missing initial_state"
    assert sm.get("states"), (
        f"{node_name} state_machine must declare at least one state"
    )
    assert sm.get("transitions"), (
        f"{node_name} state_machine must declare at least one transition"
    )


@pytest.mark.parametrize("node_name", _ALL_CONTRACTS)
def test_state_machine_passes_validate_fsm_contract(node_name: str) -> None:
    """validate_fsm_contract must return no errors for every backfilled contract."""
    contract = _load_contract_yaml(node_name)
    sm_data = contract.get("state_machine")
    assert sm_data is not None, (
        f"{node_name}: state_machine block missing - run OMN-13734 backfill first"
    )

    fsm = _build_fsm_from_yaml(sm_data)
    errors = asyncio.run(validate_fsm_contract(fsm))
    assert errors == [], f"{node_name} FSM validation errors:\n" + "\n".join(
        f"  - {e}" for e in errors
    )


@pytest.mark.parametrize("node_name", _REDUCER_CONTRACTS)
def test_model_contract_reducer_state_machine_non_none(node_name: str) -> None:
    """DoD: ModelContractReducer.state_machine is non-None for each reducer.

    The YAML block normalizes to a valid ModelFSMSubcontract, which is exactly
    the type of ModelContractReducer.state_machine; a minimal reducer contract
    built with that subcontract must expose a non-None state_machine.
    """
    from omnibase_core.enums.enum_node_type import EnumNodeType
    from omnibase_core.models.contracts.model_contract_reducer import (
        ModelContractReducer,
    )

    contract = _load_contract_yaml(node_name)
    sm_data = contract.get("state_machine")
    assert sm_data is not None, f"{node_name}: state_machine block missing"

    fsm = _build_fsm_from_yaml(sm_data)
    reducer = ModelContractReducer(
        name=f"{node_name}_contract",
        contract_version=_FSM_VERSION,
        description=f"{node_name} reducer contract (OMN-13734 backfill assertion)",
        input_model="omnimarket.models.test.ModelTestInput",
        output_model="omnimarket.models.test.ModelTestOutput",
        node_type=EnumNodeType.REDUCER_GENERIC,
        state_machine=fsm,
    )
    assert reducer.state_machine is not None, (
        f"{node_name}: ModelContractReducer.state_machine resolved to None"
    )
    assert reducer.state_machine.initial_state == sm_data["initial_state"]


# ---------------------------------------------------------------------------
# Negative test - transition referencing undeclared state must fail
# ---------------------------------------------------------------------------


def test_undeclared_to_state_transition_is_rejected_at_construction() -> None:
    """A transition referencing an undeclared state must be rejected.

    The FSM subcontract guard (`validate_transition_states_exist`) runs during
    construction and raises before `validate_fsm_contract` is ever reached, so
    an undeclared-state transition can never be encoded into a contract.
    """
    from omnibase_core.models.errors.model_onex_error import ModelOnexError

    with pytest.raises(ModelOnexError, match="nonexistent_state"):
        ModelFSMSubcontract(
            version=_FSM_VERSION,
            state_machine_name="test_bad_to_state_fsm",
            state_machine_version=_FSM_VERSION,
            description="FSM with undeclared to_state",
            states=[
                ModelFSMStateDefinition(
                    version=_FSM_VERSION,
                    state_name="idle",
                    state_type="operational",
                    description="Start state",
                ),
                ModelFSMStateDefinition(
                    version=_FSM_VERSION,
                    state_name="done",
                    state_type="terminal",
                    description="Terminal state",
                    is_terminal=True,
                    is_recoverable=False,
                ),
            ],
            initial_state="idle",
            terminal_states=["done"],
            transitions=[
                ModelFSMStateTransition(
                    version=_FSM_VERSION,
                    transition_name="idle_to_nonexistent",
                    from_state="idle",
                    to_state="nonexistent_state",  # not in states list
                    trigger="go",
                ),
            ],
        )


def test_validate_fsm_contract_flags_unreachable_state() -> None:
    """validate_fsm_contract must flag a declared-but-unreachable state.

    This exercises the reachability walk in util_fsm_executor.validate_fsm_contract
    (the path the OMN-13734 backfill must satisfy for all 15 contracts): a state
    that has no incoming transition from the initial state is reported.
    """
    fsm = ModelFSMSubcontract(
        version=_FSM_VERSION,
        state_machine_name="test_unreachable_fsm",
        state_machine_version=_FSM_VERSION,
        description="FSM with an unreachable (orphan) state",
        states=[
            ModelFSMStateDefinition(
                version=_FSM_VERSION,
                state_name="idle",
                state_type="operational",
                description="Start state",
            ),
            ModelFSMStateDefinition(
                version=_FSM_VERSION,
                state_name="done",
                state_type="terminal",
                description="Reachable terminal",
                is_terminal=True,
                is_recoverable=False,
            ),
            ModelFSMStateDefinition(
                version=_FSM_VERSION,
                state_name="orphan",
                state_type="operational",
                description="No transition ever reaches this state",
            ),
        ],
        initial_state="idle",
        terminal_states=["done"],
        transitions=[
            ModelFSMStateTransition(
                version=_FSM_VERSION,
                transition_name="idle_to_done",
                from_state="idle",
                to_state="done",
                trigger="complete",
            ),
        ],
    )

    errors = asyncio.run(validate_fsm_contract(fsm))
    assert errors, "validate_fsm_contract should flag the unreachable 'orphan' state"
    assert any("orphan" in e for e in errors), (
        f"Expected an 'Unreachable states' error mentioning 'orphan', got: {errors}"
    )
