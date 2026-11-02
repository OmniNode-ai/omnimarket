# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The announce lifecycle is the announcer contract's state_transitions block (OMN-21597, A1 AC9)."""

from pathlib import Path

import omnibase_core
import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.models.nodes.fsm_mermaid_render.model_fsm_mermaid_diagram import (
    ModelFsmMermaidDiagram,
)
from omnibase_core.utils.util_fsm_executor import execute_transition
from omnibase_core.validation.validator_fsm_analysis import analyze_fsm

from omnimarket.nodes.node_package_announce_effect.announcer_contract_loader import (
    load_announcer_contract,
)

CANONICAL_LIFECYCLE = (
    Path(omnibase_core.__file__).parent
    / "contracts"
    / "node_announce"
    / "node_announce_lifecycle.yaml"
)

# The plan's announce-lifecycle diagram (section 3.11.9), which was itself generated
# from this block; the renderer must reproduce it byte for byte.
PLAN_DIAGRAM = """\
stateDiagram-v2
    [*] --> package_found
    package_found --> contract_read: contract_file_read
    contract_read --> announce_refused: contract_invalid
    contract_read --> hashed: content_hash_computed
    hashed --> announced: announcement_published
    announced --> acknowledged: catalog_acknowledged
    announced --> hashed: acknowledgement_timeout
    announced --> announce_refused: catalog_refused
    acknowledged --> hashed: package_version_changed
    acknowledged --> withdrawn: package_removed
    withdrawn --> [*]
"""


def load_canonical_lifecycle() -> ModelFSMSubcontract:
    """Core's canonical text of the announce lifecycle."""
    raw = yaml.safe_load(CANONICAL_LIFECYCLE.read_text(encoding="utf-8"))
    return ModelFSMSubcontract.model_validate(raw["state_transitions"])


def comparable(fsm: ModelFSMSubcontract) -> dict[str, object]:
    """The machine as plain data, without the id each load generates."""
    return fsm.model_dump(mode="json", exclude={"correlation_id"})


def assert_every_state_reachable(fsm: ModelFSMSubcontract) -> None:
    """Fail when a declared state is not reachable from the initial state, or the
    initial state has an inbound edge."""
    unreachable = analyze_fsm(fsm).unreachable_states
    assert not unreachable, f"unreachable states: {unreachable}"
    inbound = [
        t.transition_name for t in fsm.transitions if t.to_state == fsm.initial_state
    ]
    assert not inbound, f"the initial state has inbound edges: {inbound}"


@pytest.mark.unit
async def test_lifecycle_loads_through_the_fsm_subcontract_model() -> None:
    fsm = load_announcer_contract().lifecycle
    assert isinstance(fsm, ModelFSMSubcontract)
    assert fsm.initial_state == "package_found"
    assert [s.state_name for s in fsm.states] == [
        "package_found",
        "contract_read",
        "hashed",
        "announced",
        "acknowledged",
        "announce_refused",
        "withdrawn",
    ]


@pytest.mark.unit
async def test_every_declared_state_is_reachable_from_the_initial_state() -> None:
    assert_every_state_reachable(load_announcer_contract().lifecycle)


@pytest.mark.unit
async def test_planted_unreachable_state_is_caught() -> None:
    """Planted violation: a state no transition leads to must fail the reachability check."""
    fsm = load_announcer_contract().lifecycle
    planted_state = fsm.states[0].model_copy(
        update={
            "state_name": "planted_orphan",
            "description": "no transition reaches me",
        }
    )
    planted = ModelFSMSubcontract.model_validate(
        fsm.model_dump(mode="json") | {"states": [*fsm.states, planted_state]}
    )
    with pytest.raises(AssertionError, match="planted_orphan"):
        assert_every_state_reachable(planted)


@pytest.mark.unit
async def test_planted_inbound_edge_to_the_initial_state_is_caught() -> None:
    fsm = load_announcer_contract().lifecycle
    loop_back = fsm.transitions[0].model_copy(
        update={
            "transition_name": "planted_loop_back",
            "from_state": "hashed",
            "to_state": fsm.initial_state,
            "trigger": "planted",
        }
    )
    planted = ModelFSMSubcontract.model_validate(
        fsm.model_dump(mode="json") | {"transitions": [*fsm.transitions, loop_back]}
    )
    with pytest.raises(AssertionError, match="initial state has inbound edges"):
        assert_every_state_reachable(planted)


@pytest.mark.unit
async def test_every_transition_fires_through_the_real_executor() -> None:
    fsm = load_announcer_contract().lifecycle
    for transition in fsm.transitions:
        result = await execute_transition(
            fsm, transition.from_state, transition.trigger, {}
        )
        assert result.success, transition.transition_name
        assert result.new_state == transition.to_state, transition.transition_name


@pytest.mark.unit
async def test_the_acknowledgement_timeout_transition_allows_three_retries() -> None:
    fsm = load_announcer_contract().lifecycle
    retry = next(t for t in fsm.transitions if t.trigger == "acknowledgement_timeout")
    assert retry.retry_enabled
    assert retry.max_retries == 3


@pytest.mark.unit
async def test_the_renderer_reproduces_the_plans_diagram_from_the_contract() -> None:
    machine = load_announcer_contract().lifecycle
    assert ModelFsmMermaidDiagram.from_machine(machine).text == PLAN_DIAGRAM


@pytest.mark.unit
async def test_a_changed_transition_changes_the_diagram() -> None:
    """The diagram is derived, so editing the contract's block edits the drawing."""
    machine = load_announcer_contract().lifecycle
    edited = ModelFSMSubcontract.model_validate(
        machine.model_dump(mode="json")
        | {
            "transitions": [
                t.model_dump(mode="json")
                | (
                    {"trigger": "renamed_trigger"}
                    if t.trigger == "package_removed"
                    else {}
                )
                for t in machine.transitions
            ]
        }
    )
    rendered = ModelFsmMermaidDiagram.from_machine(edited).text
    assert "acknowledged --> withdrawn: renamed_trigger" in rendered
    assert rendered != PLAN_DIAGRAM


@pytest.mark.unit
async def test_the_contracts_copy_equals_cores_canonical_lifecycle() -> None:
    """The announcer contract carries a copy of core's canonical text; a drifted copy fails."""
    ours = load_announcer_contract().lifecycle
    canonical = load_canonical_lifecycle()
    assert comparable(ours) == comparable(canonical)


@pytest.mark.unit
async def test_planted_drift_from_the_canonical_lifecycle_is_caught() -> None:
    """Planted violation: a copy whose retry limit differs from the canonical text."""
    canonical = load_canonical_lifecycle()
    drifted = ModelFSMSubcontract.model_validate(
        canonical.model_dump(mode="json")
        | {
            "transitions": [
                t.model_dump(mode="json")
                | ({"max_retries": 9} if t.trigger == "acknowledgement_timeout" else {})
                for t in canonical.transitions
            ]
        }
    )
    ours = load_announcer_contract().lifecycle
    assert comparable(ours) != comparable(drifted)
