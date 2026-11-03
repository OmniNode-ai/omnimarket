# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's one-runtime line shows each lane's application containers from the budget read (one-runtime plan, T2)."""

from __future__ import annotations

from pathlib import Path

import pytest

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
from tests.nodes.node_factory_floor_compute.support import NOW, ago, config

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "node_lane_container_budget_compute/fixtures/docker_ps_over_budget.ndjson"
)
HOST = "host-a"
LINE = "one-runtime"


def read(docker_ps: str) -> ModelLaneContainerBudgetVerdict:
    return HandlerLaneContainerBudget().handle(
        ModelLaneContainerBudgetRequest(
            host=HOST,
            docker_ps=docker_ps,
            declarations=(
                ModelLaneContainerBudgetDeclaration(
                    host=HOST,
                    lane="alpha",
                    compose_project="proj-alpha",
                    application_container_budget=2,
                    infrastructure_markers=("redpanda", "postgres"),
                ),
                ModelLaneContainerBudgetDeclaration(
                    host=HOST,
                    lane="beta",
                    compose_project="proj-beta",
                    application_container_budget=1,
                ),
            ),
        )
    )


def plan(name: str) -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=f"beta/plans/2026-11-01-{name}.md", title=name, tickets=("OMN-100",)
    )


def floor(
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


def line_of(result: ModelFactoryFloorResult, token: str) -> ModelFloorLine:
    return next(line for line in result.lines if token in line.line_id)


def test_one_runtime_line_shows_application_containers_per_lane_from_the_read() -> None:
    verdict = read(FIXTURE.read_text(encoding="utf-8"))
    result = floor((plan(LINE),), verdicts=(verdict,))

    shown = line_of(result, LINE).application_containers
    assert shown is not None
    assert shown.state == "read"
    rows = {(row.host, row.lane): row for row in shown.lanes}
    assert rows[(HOST, "alpha")].observed == 3
    assert rows[(HOST, "alpha")].budget == 2
    assert rows[(HOST, "alpha")].status == "FAIL"
    assert rows[(HOST, "alpha")].alarm_key == f"lane-budget:{HOST}:alpha"
    assert rows[(HOST, "beta")].observed == 1
    assert rows[(HOST, "beta")].status == "PASS"
    assert rows[(HOST, "beta")].alarm_key == ""


def test_one_runtime_line_without_a_plan_still_shows_the_read() -> None:
    verdict = read(FIXTURE.read_text(encoding="utf-8"))
    result = floor((plan("other-plan"),), verdicts=(verdict,))

    line = line_of(result, LINE)
    assert line.source == "undrawn"
    assert line.application_containers is not None
    assert line.application_containers.state == "read"
    assert len(line.application_containers.lanes) == 2


def test_one_runtime_line_with_an_unreadable_host_shows_unknown_never_a_count() -> None:
    result = floor((plan(LINE),), verdicts=(read(""),))

    shown = line_of(result, LINE).application_containers
    assert shown is not None
    assert {row.status for row in shown.lanes} == {"UNKNOWN"}
    assert {row.observed for row in shown.lanes} == {None}


def test_one_runtime_line_with_no_read_says_unread_and_other_lines_carry_none() -> None:
    result = floor((plan(LINE), plan("other-plan")))

    unread = line_of(result, LINE).application_containers
    assert unread is not None
    assert unread.state == "unread"
    assert unread.lanes == ()
    assert line_of(result, "other-plan").application_containers is None


def read_with_declared_target(
    docker_ps: str, target: int
) -> ModelLaneContainerBudgetVerdict:
    """Lane alpha declares `target`; lane beta declares none."""
    return HandlerLaneContainerBudget().handle(
        ModelLaneContainerBudgetRequest(
            host=HOST,
            docker_ps=docker_ps,
            declarations=(
                ModelLaneContainerBudgetDeclaration(
                    host=HOST,
                    lane="alpha",
                    compose_project="proj-alpha",
                    application_container_budget=2,
                    application_container_target=target,
                    infrastructure_markers=("redpanda", "postgres"),
                ),
                ModelLaneContainerBudgetDeclaration(
                    host=HOST,
                    lane="beta",
                    compose_project="proj-beta",
                    application_container_budget=1,
                ),
            ),
        )
    )


@pytest.mark.parametrize("declared", [1, 0, 7])
def test_a_declared_target_reaches_the_record_lane_row_and_none_stays_null(
    declared: int,
) -> None:
    """The record the page reads carries the declared target, taken from the declaration and never defaulted."""
    verdict = read_with_declared_target(FIXTURE.read_text(encoding="utf-8"), declared)
    result = floor((plan(LINE),), verdicts=(verdict,))

    shown = line_of(result, LINE).application_containers
    assert shown is not None
    rows = {(row.host, row.lane): row for row in shown.lanes}
    assert rows[(HOST, "alpha")].target == declared
    assert rows[(HOST, "beta")].target is None

    published = ModelFactoryFloorResult.model_validate_json(result.model_dump_json())
    published_line = line_of(published, LINE).application_containers
    assert published_line is not None
    published_rows = {row.lane: row for row in published_line.lanes}
    assert published_rows["alpha"].target == declared
    assert published_rows["beta"].target is None
