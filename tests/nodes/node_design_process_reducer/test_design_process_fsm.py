# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_design_process_reducer: the section 4.3 machine, its guards and bindings (OMN-21460)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.utils.util_fsm_executor import execute_transition
from omnibase_core.validation.validator_fsm_analysis import analyze_fsm

from omnimarket.nodes.node_design_process_reducer import NodeDesignProcessReducer
from omnimarket.nodes.node_design_process_reducer.handlers.handler_design_process import (
    CONTRACT_PATH,
    HandlerDesignProcess,
    load_fsm,
)
from omnimarket.nodes.node_design_process_reducer.models.model_design_process import (
    EnumReadbackStatus,
    ModelDesignProcessFacts,
    ModelDesignProcessRequest,
    ModelDesignProcessResult,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "section_4_3_state_machine.yaml"


def _step(state: str | None, trigger: str, **facts: object) -> ModelDesignProcessResult:
    request = ModelDesignProcessRequest(
        state=state,
        trigger=trigger,
        facts=ModelDesignProcessFacts.model_validate(facts),
        now=NOW,
    )
    return HandlerDesignProcess().handle(request)


def test_state_machine_equals_section_4_3_fragment() -> None:
    committed = yaml.safe_load(CONTRACT_PATH.read_text("utf-8"))["state_machine"]
    fragment = yaml.safe_load(FIXTURE.read_text("utf-8"))["state_machine"]
    assert committed == fragment


def test_state_machine_passes_analyze_fsm() -> None:
    result = analyze_fsm(load_fsm())
    assert not result.unreachable_states
    assert not result.dead_transitions
    assert not result.missing_transitions
    assert not result.ambiguous_transitions
    assert not result.errors


def test_contract_has_no_protocol_dependencies() -> None:
    assert "protocol_dependencies" not in yaml.safe_load(
        CONTRACT_PATH.read_text("utf-8")
    )


def test_happy_path_reaches_done_only_through_a_healthy_readback() -> None:
    state = _step(
        None,
        "live_check_declared",
        live_check_command_present=True,
        live_check_failing_reading=True,
    ).state
    assert state == "check_declared"
    state = _step(state, "failure_modes_rated", failure_mode_rows=3).state
    assert state == "failure_modes_rated"
    state = _step(state, "countermeasure_deployed", deploy_receipt_present=True).state
    assert state == "built"
    state = _step(state, "readback_scheduled").state
    assert state == "readback_pending"
    assert (
        _step(state, "readback_read", readback_status=EnumReadbackStatus.HEALTHY).state
        == "done"
    )


def test_no_readback_or_unhealthy_readback_does_not_reach_done() -> None:
    missing = _step("readback_pending", "readback_read")
    assert missing.state == "readback_pending"
    assert not missing.moved
    unhealthy = _step("readback_pending", "readback_read", readback_status="unhealthy")
    assert unhealthy.state == "failure_modes_rated"


def test_no_trigger_sequence_skips_the_readback() -> None:
    skip = _step("built", "readback_read", readback_status="healthy")
    assert skip.state == "built"
    assert not skip.moved
    assert skip.error


def test_live_check_needs_command_and_failing_reading() -> None:
    assert not _step(
        "drafted", "live_check_declared", live_check_command_present=True
    ).moved
    assert not _step(
        "drafted", "live_check_declared", live_check_failing_reading=True
    ).moved


def test_failure_modes_need_rows_and_every_row_covered() -> None:
    assert not _step("check_declared", "failure_modes_rated", failure_mode_rows=0).moved
    assert not _step(
        "check_declared",
        "failure_modes_rated",
        failure_mode_rows=2,
        rows_without_detector_or_gap=1,
    ).moved


def test_build_needs_a_deploy_receipt() -> None:
    assert not _step("failure_modes_rated", "countermeasure_deployed").moved


def test_checker_issue_refuses_and_revision_returns_to_drafted() -> None:
    refused = _step("check_declared", "record_checked", record_issue_count=2)
    assert refused.state == "refused"
    assert _step("refused", "record_revised").state == "drafted"
    assert not _step("drafted", "record_checked", record_issue_count=0).moved


def test_core_execute_transition_refuses_done_without_healthy_readback() -> None:
    fsm: ModelFSMSubcontract = load_fsm()
    result = asyncio.run(
        execute_transition(
            fsm, "readback_pending", "readback_read", {"readback_status": "pending"}
        )
    )
    assert not result.success or result.new_state != "done"


def test_handler_runs_inside_a_running_loop() -> None:
    async def run() -> str:
        return _step(
            None,
            "live_check_declared",
            live_check_command_present=True,
            live_check_failing_reading=True,
        ).state

    assert asyncio.run(run()) == "check_declared"


def test_in_process_and_entry_wrapper_are_byte_identical() -> None:
    request = ModelDesignProcessRequest(
        state="readback_pending",
        trigger="readback_read",
        facts=ModelDesignProcessFacts(readback_status=EnumReadbackStatus.HEALTHY),
        now=NOW,
    )
    assert (
        HandlerDesignProcess().handle(request).model_dump_json()
        == NodeDesignProcessReducer().handle(request).model_dump_json()
    )


def test_terminal_event_is_declared_in_the_contract() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text("utf-8"))
    terminal = contract["runtime_dispatch"]["terminal_events"]["success"]
    assert terminal == "onex.evt.omnimarket.design-process-transitioned.v1"
