# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev lane's budget of 3 judges a fourth application container as over budget (one-runtime plan, task T2).

The recorded read is h201's dev lane as of 2026-11-13: omninode-runtime, onex-api and omninode-cost-estimator-web beside
the database (``h201_dev_listing_request.json``). The dev lane's ``application_container_budget`` in the factory floor
overlay is 3, its live application count; the request fixture still declares the old 19, so each test declares the
overlay's 3 itself. The overlay lives in another repository and is not read here: the overlay's own test holds the 3,
this file holds what the node does with it.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetDeclaration,
    ModelLaneContainerBudgetRequest,
    ModelLaneContainerBudgetVerdict,
)
from omnimarket.nodes.node_lane_container_budget_compute.handlers.handler_lane_container_budget import (
    HandlerLaneContainerBudget,
)
from tests.chains.chain_assert import ChainRecorder, assert_chain

pytestmark = pytest.mark.unit

REQUESTED_TOPIC = "onex.cmd.omnimarket.lane-container-budget-requested.v1"
VERDICT_TOPIC = "onex.evt.omnimarket.lane-container-budget-judged.v1"
RECORDED_DEV_READ = Path(__file__).parent / "fixtures" / "h201_dev_listing_request.json"
DEV_BUDGET = 3
FOURTH_APPLICATION = {
    "Names": "omninode-fourth-application",
    "State": "running",
    "Labels": "com.docker.compose.project=omnibase-infra",
}


def _dev_request(
    *,
    extra_applications: tuple[dict[str, str], ...] = (),
    budget: int = DEV_BUDGET,
) -> ModelLaneContainerBudgetRequest:
    recorded = json.loads(RECORDED_DEV_READ.read_text(encoding="utf-8"))
    lines = [recorded["docker_ps"], *(json.dumps(row) for row in extra_applications)]
    declared = recorded["declarations"][0]
    return ModelLaneContainerBudgetRequest(
        host=recorded["host"],
        docker_ps="\n".join(lines),
        declarations=(
            ModelLaneContainerBudgetDeclaration(
                host=declared["host"],
                lane=declared["lane"],
                compose_project=declared["compose_project"],
                application_container_budget=budget,
                infrastructure_markers=tuple(declared["infrastructure_markers"]),
            ),
        ),
    )


async def _judge_on_the_bus(
    request: ModelLaneContainerBudgetRequest, key: str
) -> tuple[ChainRecorder, ModelLaneContainerBudgetVerdict]:
    recorder = ChainRecorder(EventBusInmemory())
    await recorder.publish(REQUESTED_TOPIC, request, correlation_id=key)
    verdict = HandlerLaneContainerBudget().handle(request)
    await recorder.publish(VERDICT_TOPIC, verdict, correlation_id=key)
    return recorder, verdict


async def test_error_chain_four_dev_applications_against_a_budget_of_3_is_fail() -> (
    None
):
    key = uuid.uuid4().hex
    recorder, verdict = await _judge_on_the_bus(
        _dev_request(extra_applications=(FOURTH_APPLICATION,)), key
    )

    reading = verdict.lanes[0]
    assert (reading.lane, reading.observed, reading.budget) == ("dev", 4, DEV_BUDGET)
    assert reading.status == "FAIL"
    assert reading.containers == (
        "omninode-cost-estimator-web",
        "omninode-fourth-application",
        "omninode-runtime",
        "onex-api",
    )
    assert verdict.status == "FAIL"
    assert verdict.failed is True
    assert [alarm.key for alarm in verdict.alarms] == ["lane-budget:h201:dev"]
    assert_chain(
        recorder.events,
        expected_event_types=[
            ModelLaneContainerBudgetRequest.__name__,
            ModelLaneContainerBudgetVerdict.__name__,
        ],
        terminal_fields={"status": "FAIL", "failed": True},
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )


async def test_control_the_recorded_three_dev_applications_against_a_budget_of_3_pass() -> (
    None
):
    key = uuid.uuid4().hex
    _, verdict = await _judge_on_the_bus(_dev_request(), key)

    reading = verdict.lanes[0]
    assert (reading.observed, reading.budget, reading.slack) == (3, DEV_BUDGET, 0)
    assert reading.status == "PASS"
    assert verdict.failed is False
    assert verdict.alarms == ()
    assert HandlerLaneContainerBudget().closing_check(verdict) == ()


async def test_a_stopped_fourth_container_is_not_an_application() -> None:
    stopped = {**FOURTH_APPLICATION, "State": "exited"}
    _, verdict = await _judge_on_the_bus(
        _dev_request(extra_applications=(stopped,)), uuid.uuid4().hex
    )

    assert verdict.lanes[0].observed == 3
    assert verdict.lanes[0].status == "PASS"


async def test_control_the_old_budget_of_19_let_a_fourth_dev_application_pass() -> None:
    _, verdict = await _judge_on_the_bus(
        _dev_request(extra_applications=(FOURTH_APPLICATION,), budget=19),
        uuid.uuid4().hex,
    )

    assert verdict.lanes[0].observed == 4
    assert verdict.lanes[0].status == "PASS"
    assert verdict.alarms == ()
