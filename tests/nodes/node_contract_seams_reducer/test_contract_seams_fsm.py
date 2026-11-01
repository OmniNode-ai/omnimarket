# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the contract seams reducer FSM."""

from datetime import UTC, datetime

import pytest
from omnibase_core.validation.validator_contract_walker import walk_contracts
from omnibase_core.validation.validator_fsm_analysis import analyze_fsm

from omnimarket.nodes.node_contract_seams_reducer.handlers.handler_contract_seams import (
    CONTRACT_PATH,
    HandlerContractSeams,
    load_fsm,
)
from omnimarket.nodes.node_contract_seams_reducer.models.model_contract_seams import (
    ModelContractSeamsFacts,
    ModelContractSeamsTransitionRequest,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 31, tzinfo=UTC)


def _step(state: str | None, trigger: str, **facts: int | bool) -> tuple[str, bool]:
    result = HandlerContractSeams().handle(
        ModelContractSeamsTransitionRequest(
            state=state,
            trigger=trigger,
            facts=ModelContractSeamsFacts.model_validate(facts),
            now=NOW,
        )
    )
    return result.state, result.moved


def test_contract_fsm_is_valid_and_walks_one_golden_path() -> None:
    assert analyze_fsm(load_fsm()).is_valid
    report = walk_contracts([CONTRACT_PATH.parent])
    assert report.summary.golden_paths == 1
    assert report.summary.dead_states == 0
    assert report.summary.unreachable_states == 0
    assert report.summary.uncovered_transitions == 0
    assert report.summary.not_armed_contracts == 0

    golden_path = next(
        path
        for workflow in report.workflows
        for path in workflow.paths
        if path.kind.value == "golden"
    )
    triggers = tuple(step.trigger for step in golden_path.steps)
    assert triggers == (
        "contracts_emitted",
        "nodes_scaffolded",
        "seams_read",
        "chains_walked",
        "golden_chain_swept",
        "chain_connected",
    )


def test_golden_run_reaches_connected() -> None:
    state, moved = _step(None, "contracts_emitted", contract_count=1)
    assert state == "contracts_written"
    assert moved

    state, moved = _step("contracts_written", "nodes_scaffolded")
    assert state == "scaffolded"
    assert moved

    state, moved = _step("scaffolded", "seams_read", seam_count=1)
    assert state == "seams_matched"
    assert moved

    state, moved = _step("seams_matched", "chains_walked", chain_count=1)
    assert state == "chains_generated"
    assert moved

    state, moved = _step("chains_generated", "golden_chain_swept", sweep_passed=True)
    assert state == "golden_chain_passed"
    assert moved

    state, moved = _step("golden_chain_passed", "chain_connected", tail_delivered=True)
    assert state == "connected"
    assert moved


@pytest.mark.parametrize(
    ("state", "trigger", "facts"),
    [
        (None, "contracts_emitted", {"contract_count": 0}),
        (None, "contracts_emitted", {"contract_count": 2, "design_issue_count": 1}),
        ("contracts_written", "nodes_scaffolded", {"unscaffolded_count": 1}),
        ("scaffolded", "seams_read", {"seam_count": 1, "seam_mismatch_count": 1}),
        ("scaffolded", "seams_read", {"seam_count": 1, "seam_unmatched_count": 1}),
        ("scaffolded", "seams_read", {"seam_count": 0}),
        ("seams_matched", "chains_walked", {"walk_issue_count": 1, "chain_count": 1}),
        (
            "seams_matched",
            "chains_walked",
            {"chain_count": 1, "undriven_path_count": 1},
        ),
        ("seams_matched", "chains_walked", {"chain_count": 0}),
        ("chains_generated", "golden_chain_swept", {"sweep_passed": False}),
        ("golden_chain_passed", "chain_connected", {"tail_delivered": False}),
    ],
)
def test_each_gate_refuses_when_its_reading_fails(
    state: str | None, trigger: str, facts: dict[str, int | bool]
) -> None:
    result_state, moved = _step(state, trigger, **facts)
    assert result_state == "refused"
    assert moved


def test_refused_returns_to_drafted_on_design_revised() -> None:
    state, moved = _step("refused", "design_revised")
    assert state == "drafted"
    assert moved


@pytest.mark.parametrize(
    "state",
    [
        "drafted",
        "contracts_written",
        "scaffolded",
        "seams_matched",
        "chains_generated",
        "golden_chain_passed",
        "refused",
    ],
)
def test_step_failed_from_any_live_state(state: str) -> None:
    result_state, moved = _step(state, "step_failed")
    assert result_state == "failed"
    assert moved


def test_terminal_states_hold() -> None:
    for terminal_state in ("connected", "failed"):
        result_state, moved = _step(terminal_state, "step_failed")
        assert result_state == terminal_state
        assert not moved


def test_unknown_trigger_holds_with_error() -> None:
    result = HandlerContractSeams().handle(
        ModelContractSeamsTransitionRequest(
            state="scaffolded",
            trigger="nodes_scaffolded",
            facts=ModelContractSeamsFacts.model_validate({}),
            now=NOW,
        )
    )
    assert result.state == "scaffolded"
    assert not result.moved
    assert result.error is not None
