# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21733: a line reads the lanes that build it and the stops that are still open, and a lane named for a task builds it."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorPlanInput,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_lanes import (
    name_tokens,
    names_task,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from tests.nodes.node_factory_floor_compute.support import (
    merge,
    pr,
    request,
    row,
)

pytestmark = pytest.mark.unit

PLAN = "beta/plans/2026-11-01-one-runtime-plan.md"
EPIC = "OMN-50"


def scoped(
    task_id: str, *tickets: str, prs: tuple[str, ...] = ()
) -> ModelFloorManifestTask:
    return ModelFloorManifestTask(
        id=task_id, title=f"{task_id} work", tickets=tickets, prs=prs, scoped=True
    )


def plan_of(*tasks: ModelFloorManifestTask) -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=PLAN,
        title="One runtime",
        tickets=(EPIC,),
        manifest=ModelFloorManifest(tasks=tasks),
    )


def claim(lane: str, minutes_ago: float = 20, **cells: Any) -> Any:
    return row("CLAIM", minutes_ago, lane, tickets=(EPIC,), **cells)


def test_a_lane_name_is_read_as_whole_words_of_the_task_id() -> None:
    tokens = name_tokens("one-rt-t10-delegation-1101")
    assert names_task("T10", tokens)
    assert not names_task("T1", tokens)
    assert not names_task("T100", tokens)
    assert names_task("T0.1", name_tokens("val-t0-1-vocab"))
    assert not names_task("", tokens)


def test_a_lane_named_for_a_task_builds_it_and_the_station_shows_the_lane() -> None:
    result = compute_factory_floor(
        request(
            plans=(plan_of(scoped("T10"), scoped("T15"), scoped("T1")),),
            ledger=(
                claim("one-rt-t10-delegation-1101"),
                claim("one-rt-t15-http-mount-1101"),
            ),
        )
    )
    states = {i.id: i.state for st in result.lines[0].stations for i in st.items}
    assert states == {"T10": "active", "T15": "active", "T1": "prep"}
    by_lane = {lane.lane: lane.station for lane in result.floor.lanes}
    assert by_lane["one-rt-t10-delegation-1101"].item_id == "T10"
    assert by_lane["one-rt-t15-http-mount-1101"].item_id == "T15"


def test_a_lane_of_another_plan_with_the_same_word_builds_nothing_here() -> None:
    result = compute_factory_floor(
        request(
            plans=(plan_of(scoped("T10")),),
            ledger=(row("CLAIM", 20, "other-t10-lane", tickets=("OMN-999",)),),
        )
    )
    assert result.lines[0].stations[0].items[0].state == "prep"
    assert all(lane.station is None for lane in result.floor.lanes)


def blocked_stop(**cells: Any) -> Any:
    return row(
        "TERMINAL",
        300,
        "ended-lane",
        outcome="blocked",
        reason="the gate refused",
        **{"tickets": (EPIC,), **cells},
    )


def test_a_stop_on_the_plans_own_ticket_stops_the_line_when_no_lane_works_on_it() -> (
    None
):
    result = compute_factory_floor(
        request(plans=(plan_of(scoped("T3", "OMN-3")),), ledger=(blocked_stop(),))
    )
    line = result.lines[0]
    assert line.status_pill == "stopped"
    assert line.andon.flag
    assert not line.andon.hard
    assert "the gate refused" in line.andon.causes[0]


def test_a_live_lane_on_the_line_keeps_it_building_with_its_stop_still_listed() -> None:
    result = compute_factory_floor(
        request(
            plans=(plan_of(scoped("T3", "OMN-3"), scoped("T10")),),
            ledger=(
                blocked_stop(tickets=("OMN-3",)),
                claim("one-rt-t10-delegation-1101", minutes_ago=5),
            ),
        )
    )
    line = result.lines[0]
    assert line.status_pill == "building"
    assert line.andon.flag
    assert line.andon.causes


def test_a_hold_stops_the_line_whatever_runs_on_it() -> None:
    hold = row(
        "HOLD",
        60,
        "orchestrator",
        tickets=(EPIC,),
        reason="andon: wrong shape",
        text="andon: wrong shape",
    )
    result = compute_factory_floor(
        request(
            plans=(plan_of(scoped("T10")),),
            ledger=(hold, claim("one-rt-t10-delegation-1101", minutes_ago=5)),
        )
    )
    line = result.lines[0]
    assert line.andon.hard
    assert line.status_pill == "stopped"


def test_a_stalled_lane_does_not_keep_a_stopped_line_building() -> None:
    result = compute_factory_floor(
        request(
            plans=(plan_of(scoped("T3", "OMN-3"), scoped("T10")),),
            ledger=(
                blocked_stop(tickets=("OMN-3",)),
                claim("one-rt-t10-delegation-1101", minutes_ago=100),
            ),
        )
    )
    lane = next(lane for lane in result.floor.lanes if lane.stalled)
    assert lane.lane == "one-rt-t10-delegation-1101"
    assert result.lines[0].status_pill == "stopped"


def test_a_stop_is_over_once_the_pull_request_it_names_has_merged() -> None:
    ledger = (
        blocked_stop(prs=("repo#7",)),
        row(
            "MSG",
            1296,
            "asker",
            to="orchestrator-31d9",
            row_id="m-1",
            prs=("OmniNode-ai/repo#7",),
            tickets=(EPIC,),
            text="repo#7 has a new head",
        ),
    )
    merged = request(
        plans=(plan_of(scoped("T3", "OMN-3")),),
        ledger=ledger,
        prs=(pr("repo#7", "MERGED"),),
        merges=(merge("repo#7", 30),),
    )
    assert compute_factory_floor(merged).lines[0].andon.causes == ()
    open_pr = request(
        plans=(plan_of(scoped("T3", "OMN-3")),),
        ledger=ledger,
        prs=(pr("repo#7", "OPEN"),),
    )
    assert len(compute_factory_floor(open_pr).lines[0].andon.causes) == 2


def test_a_stop_on_a_ticket_whose_parts_are_all_done_is_over() -> None:
    done_part = scoped("T3", "OMN-3", prs=("repo#9",))
    result = compute_factory_floor(
        request(
            plans=(plan_of(done_part),),
            ledger=(
                row(
                    "TERMINAL",
                    300,
                    "ended-lane",
                    outcome="blocked",
                    reason="premise failed",
                    tickets=("OMN-3",),
                ),
            ),
            prs=(pr("repo#9", "MERGED"),),
            merges=(merge("repo#9", 30),),
        )
    )
    line = result.lines[0]
    assert line.andon.causes == ()
    assert line.status_pill == "done"


def handed_off(*prs: str) -> Any:
    return row(
        "TERMINAL",
        60,
        "one-rt-t10-delegation-1101",
        outcome="handed-off",
        tickets=(EPIC,),
        prs=prs,
    )


def test_a_hand_off_is_active_until_its_pull_request_merges_never_done() -> None:
    def state(**kwargs: Any) -> tuple[str, str]:
        result = compute_factory_floor(
            request(
                plans=(plan_of(scoped("T10"), scoped("T1")),),
                ledger=(handed_off("omnimarket#4799"),),
                **kwargs,
            )
        )
        item = result.lines[0].stations[0].items[0]
        return item.state, item.note

    waiting = (
        "active",
        "handed to landing by one-rt-t10-delegation-1101: omnimarket#4799",
    )
    assert state(prs=(pr("omnimarket#4799", "OPEN"),)) == waiting
    assert state() == waiting
    merged = state(
        prs=(pr("omnimarket#4799", "MERGED"),), merges=(merge("omnimarket#4799", 5),)
    )
    assert merged == ("done", "PRs merged")


def test_a_hand_off_adds_nothing_to_the_percent_built() -> None:
    result = compute_factory_floor(
        request(
            plans=(plan_of(scoped("T10"), scoped("T15")),),
            ledger=(handed_off("omnimarket#4799"),),
        )
    )
    assert result.lines[0].percent_built == 0
