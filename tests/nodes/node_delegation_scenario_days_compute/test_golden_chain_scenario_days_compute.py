# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the scenario green-days compute node (OMN-23171): contract, one counted history, one unread history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib.resources import files

import pytest
import yaml

from omnimarket.models.model_delegation_scenario import (
    EnumScenarioCadence,
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioDeclaration,
    ModelScenarioHistory,
    ModelScenarioResult,
)
from omnimarket.nodes.node_delegation_scenario_days_compute.handlers.handler_scenario_green_days import (
    HandlerScenarioGreenDays,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.delegation-scenario-green-days-computed.v1"
AS_OF = datetime(2026, 11, 9, 12, 30, tzinfo=UTC)
DAILY = ModelScenarioDeclaration(
    scenario_id="R6", cadence=EnumScenarioCadence.DAILY, lane="lane-r6"
)


def _daily(offset: int) -> ModelScenarioResult:
    slot = AS_OF.replace(hour=0, minute=0) - timedelta(days=offset)
    return ModelScenarioResult(
        scenario_id="R6",
        slot_start=slot,
        trigger=EnumScenarioTrigger.SCHEDULE,
        conclusion=EnumScenarioConclusion.SUCCESS,
        recorded_at=slot + timedelta(minutes=5),
    )


def test_golden_chain_scenario_days_compute_declares_its_topics() -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_delegation_scenario_days_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    assert contract["terminal_event"] == TERMINAL
    assert TERMINAL in contract["event_bus"]["publish_topics"]
    assert TERMINAL in contract["externally_consumed_topics"]
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.delegation-scenario-green-days-compute-requested.v1"
    )


def test_golden_chain_scenario_days_compute_counts_three_daily_green_days() -> None:
    history = ModelScenarioHistory(
        as_of=AS_OF, scenarios=(DAILY,), results=tuple(_daily(n) for n in (1, 2, 3))
    )
    row = HandlerScenarioGreenDays().handle(history).by_id()["R6"]
    assert (row.green_days, row.readable, row.red) == (3, True, False)


def test_error_chain_scenario_days_compute_an_unread_history_is_red() -> None:
    history = ModelScenarioHistory(
        as_of=AS_OF,
        scenarios=(DAILY,),
        results=tuple(_daily(n) for n in (1, 2, 3)),
        read_complete=False,
        read_error="page 2 did not answer",
    )
    row = HandlerScenarioGreenDays().handle(history).by_id()["R6"]
    assert (row.green_days, row.readable, row.red) == (0, False, True)
