# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pr-land fallback in the lab-fill dispatch plan and its lane brief (OMN-20864)."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_dispatch_plan import (
    HandlerLabFillDispatchPlan,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_lane_render import (
    HandlerLabFillLaneRender,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCapacityResult,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillHostCapacity,
    ModelLabFillPlanConfig,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models.model_lab_fill_lane_render import (
    ModelLabFillLaneRenderRequest,
    ModelLabFillRenderConfig,
)

pytestmark = pytest.mark.unit


def _land(number: int, **over: Any) -> dict[str, object]:
    return {
        "kind": "pr-land",
        "ticket": "",
        "pr": f"repo_a#{number}",
        "repo": "repo_a",
        "head_ref": f"b{number}",
        "title": f"parked: change {number}",
        **over,
    }


def _plan(candidates: list[dict[str, object]], lanes: int, **cfg: Any) -> Any:
    host = ModelLabFillHostCapacity(
        name="h201",
        lanes=lanes,
        idle_lanes=lanes,
        runner_slots=lanes,
        cap_bound=False,
        codex=True,
        claude=True,
        running=0,
        load_per_core=0.1,
        reason="",
    )
    return HandlerLabFillDispatchPlan().handle(
        ModelLabFillDispatchPlanRequest(
            candidates=tuple(candidates),
            capacity=ModelLabFillCapacityResult(
                hosts=(host,), free=lanes, budget=lanes
            ),
            config=ModelLabFillPlanConfig(
                parent_ticket="OMN-20864", milestone="m", run_key="2026-10-10", **cfg
            ),
        )
    )


def test_pr_land_items_take_only_slots_ordinary_work_left_idle() -> None:
    ordinary = {
        "kind": "ticket",
        "ticket": "OMN-5",
        "under_parent": True,
        "title": "t",
    }

    plan = _plan([_land(1), ordinary, _land(2)], lanes=2)

    assert [(i.kind, i.pr or i.ticket) for i in plan.dispatch] == [
        ("ticket", "OMN-5"),
        ("pr-land", "repo_a#1"),
    ]
    assert [(d.id, d.reason) for d in plan.deferred] == [("repo_a#2", "over-cap")]


def test_pr_land_items_without_a_ticket_take_the_parent_and_are_not_deduped_by_it() -> (
    None
):
    plan = _plan([_land(1), _land(2)], lanes=2)

    assert [(i.lane, i.ticket, i.engine) for i in plan.dispatch] == [
        ("lab-fill-land-repo_a-1-1010", "OMN-20864", "sonnet"),
        ("lab-fill-land-repo_a-2-1010", "OMN-20864", "sonnet"),
    ]


def test_a_pr_red_candidate_wins_over_a_pr_land_item_for_the_same_pr() -> None:
    red = {
        "kind": "pr-red",
        "ticket": "OMN-9",
        "pr": "repo_a#1",
        "repo": "repo_a",
        "under_parent": True,
    }

    plan = _plan([_land(1), red], lanes=2)

    assert [(i.kind, i.pr) for i in plan.dispatch] == [("pr-red", "repo_a#1")]
    assert [(s.id, s.reason) for s in plan.skipped] == [("repo_a#1", "duplicate")]


def test_pr_land_respects_the_project_pr_repositories() -> None:
    plan = _plan(
        [_land(1)], lanes=1, project_id="p", operator_id="o", pr_repos=("repo_b",)
    )

    assert plan.dispatch == ()
    assert [(s.id, s.reason) for s in plan.skipped] == [("repo_a#1", "out-of-scope")]


def test_a_pr_land_lane_brief_runs_pr_land_and_reports_an_unfixable_cause() -> None:
    item = _plan([_land(1)], lanes=1).dispatch[0]

    rendered = HandlerLabFillLaneRender().handle(
        ModelLabFillLaneRenderRequest(
            item=item,
            config=ModelLabFillRenderConfig(
                pillar="landing",
                parent_lane="lab-fill",
                parent_ticket="OMN-20864",
                run_key="2026-10-10",
                authority_ruling="2026-10-10T04:08:23Z",
                authority_lane="ruling-idle-lab-9f8a",
            ),
        )
    )

    assert "TASK: Land repo_a#1 (OMN-20864) with /omni:pr-land" in rendered.brief
    assert "cause=<required-approval|base-red|held|external-owner>" in rendered.brief
    assert "no merge by hand, no arming" not in rendered.brief
    assert rendered.launch.pr == "repo_a#1"
    assert rendered.launch.lane == "lab-fill-land-repo_a-1-1010"
