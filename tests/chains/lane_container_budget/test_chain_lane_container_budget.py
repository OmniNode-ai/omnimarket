# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the lane container budget read (one-runtime plan, task T2).

The node under test is a pure compute node: it takes a host's recorded container listing and the budgets declared for
it and returns a verdict. It declares no topics of its own, so the chain is the request event and the verdict event,
published by the test on in-memory topics that stand in for the contract's two: the request topic
``onex.cmd.omnimarket.lane-container-budget-requested.v1`` and the verdict topic
``onex.evt.omnimarket.lane-container-budget-judged.v1``.

Golden: the request is published, the node judges the listing, and the verdict is published. Error: over budget is
FAIL and raises one alarm per failing lane; a host that was not read is UNKNOWN, which fails too. The verdict's bytes
are the floor's input: the floor validates them with the verdict's own model, so a shape change on either side fails.

The seam with the floor is a shared model, not a topic: the node publishes nothing to a broker, so no broker test
belongs in this file.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorPlanInput,
    ModelFloorSelection,
    ModelFloorWatcher,
)
from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetDeclaration,
    ModelLaneContainerBudgetRequest,
    ModelLaneContainerBudgetVerdict,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFactoryFloorResult,
    ModelFloorLine,
)
from omnimarket.nodes.node_lane_container_budget_compute.handlers.handler_lane_container_budget import (
    HandlerLaneContainerBudget,
)
from tests.chains.chain_assert import ChainRecorder, assert_chain
from tests.nodes.node_factory_floor_compute.support import NOW, ago, config

pytestmark = pytest.mark.unit

REQUESTED_TOPIC = "onex.cmd.omnimarket.lane-container-budget-requested.v1"
VERDICT_TOPIC = "onex.evt.omnimarket.lane-container-budget-judged.v1"
FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "nodes/node_lane_container_budget_compute/fixtures/docker_ps_over_budget.ndjson"
)
HOST = "host-a"
LINE = "one-runtime"


def _declaration(budget: int) -> ModelLaneContainerBudgetDeclaration:
    return ModelLaneContainerBudgetDeclaration(
        host=HOST,
        lane="alpha",
        compose_project="proj-alpha",
        application_container_budget=budget,
        infrastructure_markers=("redpanda", "postgres"),
    )


def _request(docker_ps: str, budget: int) -> ModelLaneContainerBudgetRequest:
    return ModelLaneContainerBudgetRequest(
        host=HOST,
        docker_ps=docker_ps,
        declarations=(_declaration(budget),),
    )


async def _run(
    docker_ps: str, budget: int, key: str
) -> tuple[ChainRecorder, ModelLaneContainerBudgetVerdict]:
    recorder = ChainRecorder(EventBusInmemory())
    request = _request(docker_ps, budget)
    await recorder.publish(REQUESTED_TOPIC, request, correlation_id=key)
    verdict = HandlerLaneContainerBudget().handle(request)
    await recorder.publish(VERDICT_TOPIC, verdict, correlation_id=key)
    return recorder, verdict


def _plan(name: str) -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=f"beta/plans/2026-11-01-{name}.md", title=name, tickets=("OMN-100",)
    )


def _floor(
    plans: tuple[ModelFloorPlanInput, ...],
    *,
    verdicts: tuple[ModelLaneContainerBudgetVerdict, ...] | None = None,
) -> ModelFactoryFloorResult:
    return compute_factory_floor(
        ModelFactoryFloorRequest(
            generated_at=NOW,
            config=config(),
            selection=ModelFloorSelection(
                plan_path_contains=(LINE, "other-plan"), container_read_line=LINE
            ),
            plans=plans,
            watcher=ModelFloorWatcher(last_tick=ago(3)),
            lane_containers=verdicts,
        )
    )


def _line_of(result: ModelFactoryFloorResult, token: str) -> ModelFloorLine:
    return next(line for line in result.lines if token in line.line_id)


async def test_golden_chain_request_then_pass_verdict_in_memory() -> None:
    key = uuid.uuid4().hex
    recorder, verdict = await _run(FIXTURE.read_text(encoding="utf-8"), 3, key)
    assert verdict.status == "PASS"
    assert verdict.failed is False
    assert verdict.alarms == ()
    assert_chain(
        recorder.events,
        expected_event_types=[
            ModelLaneContainerBudgetRequest.__name__,
            ModelLaneContainerBudgetVerdict.__name__,
        ],
        terminal_fields={"status": "PASS", "failed": False},
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )


async def test_error_chain_over_budget_publishes_one_fail_alarm_in_memory() -> None:
    key = uuid.uuid4().hex
    listing = FIXTURE.read_text(encoding="utf-8")
    handler = HandlerLaneContainerBudget()
    rows: tuple = ()
    for _ in range(2):
        recorder, verdict = await _run(listing, 2, key)
        assert verdict.status == "FAIL"
        assert verdict.failed is True
        assert [alarm.key for alarm in verdict.alarms] == [f"lane-budget:{HOST}:alpha"]
        rows = handler.upsert_alarms(rows, verdict)
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
    assert len(rows) == 1


async def test_error_chain_unreadable_host_publishes_unknown_in_memory() -> None:
    key = uuid.uuid4().hex
    recorder, verdict = await _run("", 3, key)
    assert verdict.status == "UNKNOWN"
    assert verdict.failed is True
    assert verdict.alarms[0].status == "UNKNOWN"
    assert_chain(
        recorder.events,
        expected_event_types=[
            ModelLaneContainerBudgetRequest.__name__,
            ModelLaneContainerBudgetVerdict.__name__,
        ],
        terminal_fields={"status": "UNKNOWN", "failed": True},
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )


async def test_seam_verdict_bytes_are_the_floor_input_shape() -> None:
    key = uuid.uuid4().hex
    recorder, verdict = await _run(FIXTURE.read_text(encoding="utf-8"), 3, key)
    history = await recorder._bus.get_event_history(limit=100)
    raw = next(json.loads(bytes(m.value)) for m in history if m.topic == VERDICT_TOPIC)
    validated = ModelLaneContainerBudgetVerdict.model_validate(raw)
    result = _floor((_plan(LINE),), verdicts=(validated,))
    shown = _line_of(result, LINE).application_containers
    assert shown is not None
    rows = {(row.host, row.lane): row for row in shown.lanes}
    assert rows[(HOST, "alpha")].observed == 3
    assert rows[(HOST, "alpha")].budget == 3
    assert rows[(HOST, "alpha")].status == "PASS"
    assert verdict.lanes[0].slack == 0


async def test_seam_a_wide_budget_shows_slack_and_the_closing_check_names_it() -> None:
    key = uuid.uuid4().hex
    _, verdict = await _run(FIXTURE.read_text(encoding="utf-8"), 19, key)
    assert verdict.lanes[0].slack == 16
    named = HandlerLaneContainerBudget().closing_check(verdict)
    assert [lane.lane for lane in named] == ["alpha"]
    result = _floor((_plan(LINE),), verdicts=(verdict,))
    shown = _line_of(result, LINE).application_containers
    assert shown is not None
    rows = {(row.host, row.lane): row for row in shown.lanes}
    assert rows[(HOST, "alpha")].observed == 3
    assert rows[(HOST, "alpha")].budget == 19
