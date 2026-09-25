# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Walkable contract state machines (OMN-19547, epic OMN-19546).

Golden-chain validation layer plan r4, Phase -1 deliverables 1, 2 and 8: a
contract machine is walkable only when its ``state_machine:`` block loads
directly into ``omnibase_core``'s ``ModelFSMSubcontract``, with no key the model
does not declare. The FSM model family is ``extra="ignore"`` at every level, so
a misspelt key (``to_sate:``) would otherwise load clean and vanish; the strict
key check here refuses it instead.

A contract opts in by declaring ``state_machine_version`` in its
``state_machine:`` block. Every opted-in contract must:

1. contain only keys the typed models declare (strict schema);
2. load through ``ModelFSMSubcontract.model_validate``;
3. pass ``analyze_fsm``: no unreachable states, no dead transitions, no
   non-terminal state without an exit, no cycle without an exit, no ambiguous
   transition.

Contracts that declare a machine but are not yet in the typed form are
``NOT_ARMED``. They are listed below by node and must never silently drop out:
the list may only shrink, and a node on it that has since been converted fails
the test until it is removed from the list.
"""

from __future__ import annotations

from pathlib import Path

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
from omnibase_core.models.fsm.model_fsm_operation import ModelFSMOperation
from omnibase_core.models.fsm.model_fsm_transition_action import (
    ModelFSMTransitionAction,
)
from omnibase_core.models.fsm.model_fsm_transition_condition import (
    ModelFSMTransitionCondition,
)
from omnibase_core.models.primitives.model_semver import ModelSemVer
from omnibase_core.validation.validator_fsm_analysis import analyze_fsm
from pydantic import BaseModel

_SRC = Path(__file__).resolve().parents[3] / "src"

# NOT_ARMED: nodes whose contract declares a machine that is not yet in the
# typed, directly loadable form. Shrink-only. Converting one means removing it
# here in the same change.
_NOT_ARMED: dict[str, str] = {
    # state_machine: without versions, state types or transition names - OMN-19549
    "node_ab_compare_reducer": "state_machine: not directly loadable",
    "node_canary_score_reducer": "state_machine: not directly loadable",
    "node_contract_reducer": "state_machine: not directly loadable",
    "node_deployment_evidence_reducer": "state_machine: not directly loadable",
    "node_evidence_dashboard_reducer": "state_machine: not directly loadable",
    "node_intelligence_reducer": "state_machine: not directly loadable",
    "node_knowledge_context_assembler_reducer": "state_machine: not directly loadable",
    "node_ledger_state_reducer": "state_machine: not directly loadable",
    "node_loop_state_reducer": "state_machine: not directly loadable",
    "node_merge_sweep_state_reducer": "state_machine: not directly loadable",
    "node_navigation_history_reducer": "state_machine: not directly loadable",
    "node_pr_lifecycle_state_reducer": "state_machine: not directly loadable",
    "node_pr_review_fsm_reducer": "state_machine: not directly loadable",
    "node_session_phase_reducer": "state_machine: not directly loadable",
    "node_swarm_subtask_state_reducer": "state_machine: not directly loadable",
}


def _declared_keys(model: type[BaseModel]) -> frozenset[str]:
    return frozenset(name for name in model.model_fields if not name.isupper())


def _unknown_keys(block: object) -> list[str]:
    """Return every key in a raw ``state_machine`` mapping the models do not declare."""
    found: list[str] = []

    def check(
        raw: object,
        model: type[BaseModel],
        where: str,
        nested: dict[str, tuple[type[BaseModel], bool]],
    ) -> None:
        if not isinstance(raw, dict):
            found.append(f"{where}: expected a mapping, got {type(raw).__name__}")
            return
        allowed = _declared_keys(model)
        for key, value in raw.items():
            if key not in allowed:
                found.append(f"{where}.{key}")
                continue
            if key in nested:
                child, is_list = nested[key]
                if is_list and isinstance(value, list):
                    for index, item in enumerate(value):
                        check(item, child, f"{where}.{key}[{index}]", {})
                elif not is_list:
                    check(value, child, f"{where}.{key}", {})

    if not isinstance(block, dict):
        return ["state_machine: expected a mapping"]
    check(
        block,
        ModelFSMSubcontract,
        "state_machine",
        {
            "version": (ModelSemVer, False),
            "state_machine_version": (ModelSemVer, False),
            "operations": (ModelFSMOperation, True),
        },
    )
    for index, state in enumerate(block.get("states") or []):
        check(
            state,
            ModelFSMStateDefinition,
            f"state_machine.states[{index}]",
            {"version": (ModelSemVer, False)},
        )
    for index, transition in enumerate(block.get("transitions") or []):
        check(
            transition,
            ModelFSMStateTransition,
            f"state_machine.transitions[{index}]",
            {
                "version": (ModelSemVer, False),
                "conditions": (ModelFSMTransitionCondition, True),
                "actions": (ModelFSMTransitionAction, True),
            },
        )
    return found


def _analysis_defects(fsm: ModelFSMSubcontract) -> list[str]:
    """Return analyze_fsm findings that make a machine unwalkable.

    A machine that declares no terminal state is a perpetual accumulator (a
    projection-style reducer that folds events forever). Its self-loops are
    exitless by declaration, so analyze_fsm's exitless-cycle finding is expected
    there and is not a defect. Every other finding still is. A machine that
    declares a terminal state gets no such exemption.
    """
    result = analyze_fsm(fsm)
    perpetual = not fsm.terminal_states
    defects: list[str] = []
    if result.unreachable_states:
        defects.append(f"unreachable states: {result.unreachable_states}")
    if result.dead_transitions:
        defects.append(f"dead transitions: {result.dead_transitions}")
    if result.missing_transitions:
        defects.append(
            f"non-terminal states with no exit: {result.missing_transitions}"
        )
    if result.cycles_without_exit and not perpetual:
        defects.append(f"cycles without exit: {result.cycles_without_exit}")
    if result.ambiguous_transitions:
        defects.append(f"ambiguous transitions: {result.ambiguous_transitions}")
    if result.duplicate_state_names:
        defects.append(f"duplicate states: {result.duplicate_state_names}")
    errors = [
        e
        for e in result.errors
        if not (perpetual and e.startswith("Found cycle without exit"))
    ]
    if errors:
        defects.append(f"errors: {errors}")
    return defects


def _contracts() -> list[tuple[str, dict[str, object]]]:
    found: list[tuple[str, dict[str, object]]] = []
    for path in sorted(_SRC.rglob("contract.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            found.append((path.parent.name, data))
    return found


def _typed_machines() -> list[tuple[str, dict[str, object]]]:
    return [
        (node, data["state_machine"])  # type: ignore[misc]
        for node, data in _contracts()
        if isinstance(data.get("state_machine"), dict)
        and "state_machine_version" in data["state_machine"]  # type: ignore[operator]
    ]


def _machine_nodes_not_armed() -> set[str]:
    nodes: set[str] = set()
    for node, data in _contracts():
        block = data.get("state_machine")
        if isinstance(data.get("fsm"), dict) or (
            isinstance(block, dict) and "state_machine_version" not in block
        ):
            nodes.add(node)
    return nodes


_TYPED = _typed_machines()


@pytest.mark.unit
def test_at_least_one_typed_machine_is_discovered() -> None:
    """Positive control: discovery finds the delegation orchestrator's machine."""
    assert "node_delegation_orchestrator" in {node for node, _ in _TYPED}


@pytest.mark.unit
@pytest.mark.parametrize(("node", "block"), _TYPED, ids=[node for node, _ in _TYPED])
def test_typed_machine_has_no_unknown_keys(node: str, block: dict[str, object]) -> None:
    unknown = _unknown_keys(block)
    assert not unknown, f"{node}: keys the typed FSM models do not declare: {unknown}"


@pytest.mark.unit
@pytest.mark.parametrize(("node", "block"), _TYPED, ids=[node for node, _ in _TYPED])
def test_typed_machine_loads_and_walks_clean(
    node: str, block: dict[str, object]
) -> None:
    fsm = ModelFSMSubcontract.model_validate(block)
    defects = _analysis_defects(fsm)
    assert not defects, f"{node}: analyze_fsm findings: {defects}"


@pytest.mark.unit
def test_delegation_machine_labels_failed_as_error_state() -> None:
    block = dict(_TYPED)["node_delegation_orchestrator"]
    fsm = ModelFSMSubcontract.model_validate(block)
    assert fsm.error_states == ["FAILED"]
    assert set(fsm.terminal_states) == {"COMPLETED", "FAILED"}


@pytest.mark.unit
def test_not_armed_list_matches_contracts_exactly() -> None:
    """NOT_ARMED contracts never disappear, and converted ones leave the list."""
    actual = _machine_nodes_not_armed()
    unlisted = sorted(actual - set(_NOT_ARMED))
    converted = sorted(set(_NOT_ARMED) - actual)
    assert not unlisted, (
        f"contracts declare a machine that is not in the typed form and are not "
        f"listed as NOT_ARMED (write the typed form instead): {unlisted}"
    )
    assert not converted, (
        f"listed as NOT_ARMED but now typed or without a machine; remove them from "
        f"_NOT_ARMED: {converted}"
    )


_GOOD_FIXTURE: dict[str, object] = {
    "version": {"major": 1, "minor": 0, "patch": 0},
    "state_machine_name": "fixture",
    "state_machine_version": {"major": 1, "minor": 0, "patch": 0},
    "description": "fixture",
    "initial_state": "A",
    "terminal_states": ["B"],
    "error_states": [],
    "states": [
        {
            "version": {"major": 1, "minor": 0, "patch": 0},
            "state_name": "A",
            "state_type": "operational",
            "description": "a",
        },
        {
            "version": {"major": 1, "minor": 0, "patch": 0},
            "state_name": "B",
            "state_type": "terminal",
            "description": "b",
            "is_terminal": True,
            "is_recoverable": False,
        },
    ],
    "transitions": [
        {
            "version": {"major": 1, "minor": 0, "patch": 0},
            "transition_name": "a_to_b",
            "from_state": "A",
            "to_state": "B",
            "trigger": "go",
        }
    ],
}


@pytest.mark.unit
def test_fixture_control_is_clean() -> None:
    assert _unknown_keys(_GOOD_FIXTURE) == []
    assert _analysis_defects(ModelFSMSubcontract.model_validate(_GOOD_FIXTURE)) == []


@pytest.mark.unit
def test_unknown_key_misspelt_transition_key_is_refused() -> None:
    """A misspelt key loads clean through the permissive model; the strict check refuses it."""
    bad = dict(_GOOD_FIXTURE)
    transition = dict(bad["transitions"][0])  # type: ignore[index]
    transition["conditons"] = []
    bad["transitions"] = [transition]
    ModelFSMSubcontract.model_validate(bad)  # the permissive model accepts it
    assert _unknown_keys(bad) == ["state_machine.transitions[0].conditons"]


@pytest.mark.unit
def test_unknown_key_misspelt_top_level_and_state_keys_are_refused() -> None:
    bad = dict(_GOOD_FIXTURE)
    bad["error_state"] = ["B"]
    state = dict(bad["states"][0])  # type: ignore[index]
    state["is_termnal"] = True
    bad["states"] = [state, bad["states"][1]]  # type: ignore[index]
    assert sorted(_unknown_keys(bad)) == [
        "state_machine.error_state",
        "state_machine.states[0].is_termnal",
    ]


@pytest.mark.unit
def test_unreachable_state_is_reported() -> None:
    bad = dict(_GOOD_FIXTURE)
    bad["states"] = [
        *bad["states"],  # type: ignore[misc]
        {
            "version": {"major": 1, "minor": 0, "patch": 0},
            "state_name": "ORPHAN",
            "state_type": "terminal",
            "description": "never entered",
            "is_terminal": True,
            "is_recoverable": False,
        },
    ]
    bad["terminal_states"] = ["B", "ORPHAN"]
    defects = _analysis_defects(ModelFSMSubcontract.model_validate(bad))
    assert any("ORPHAN" in d for d in defects), defects


_EXITLESS_LOOP: dict[str, object] = {
    **_GOOD_FIXTURE,
    "transitions": [
        *_GOOD_FIXTURE["transitions"],  # type: ignore[misc]
        {
            "version": {"major": 1, "minor": 0, "patch": 0},
            "transition_name": "a_to_c",
            "from_state": "A",
            "to_state": "C",
            "trigger": "stall",
        },
        {
            "version": {"major": 1, "minor": 0, "patch": 0},
            "transition_name": "c_to_c",
            "from_state": "C",
            "to_state": "C",
            "trigger": "tick",
        },
    ],
    "states": [
        *_GOOD_FIXTURE["states"],  # type: ignore[misc]
        {
            "version": {"major": 1, "minor": 0, "patch": 0},
            "state_name": "C",
            "state_type": "operational",
            "description": "c",
        },
    ],
}


@pytest.mark.unit
def test_exitless_cycle_is_reported_when_machine_declares_a_terminal_state() -> None:
    defects = _analysis_defects(ModelFSMSubcontract.model_validate(_EXITLESS_LOOP))
    assert any("cycles without exit" in d for d in defects), defects


@pytest.mark.unit
def test_exitless_cycle_is_expected_in_a_perpetual_machine() -> None:
    perpetual = {**_EXITLESS_LOOP, "terminal_states": []}
    perpetual["states"] = [
        {k: v for k, v in state.items() if k not in {"is_terminal", "is_recoverable"}}
        | {"state_type": "operational"}
        for state in _EXITLESS_LOOP["states"]  # type: ignore[attr-defined]
    ]
    fsm = ModelFSMSubcontract.model_validate(perpetual)
    assert not any("cycle" in d for d in _analysis_defects(fsm))
