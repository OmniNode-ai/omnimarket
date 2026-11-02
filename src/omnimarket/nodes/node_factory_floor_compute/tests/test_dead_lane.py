# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane whose last row is a TERMINAL older than the stall window stops no line (OMN-21930)."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorPlanInput,
    ModelFloorTableTask,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from tests.nodes.node_factory_floor_compute.support import config, request, row

pytestmark = pytest.mark.unit

CONTEXT = "beta/plans/2026-10-01-ended-lane-plan.md"
EPIC = "OMN-16928"
# plan-r8-0605 ended blocked 9918 minutes before the live floor of 2026-11-02T01:21:53Z was read.
DEAD_MINUTES = 9918


def context_plan() -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=CONTEXT,
        title="Context and delegation adapter",
        tickets=(EPIC,),
        table_tasks=(
            ModelFloorTableTask(label="R1", text="first", state="not-started"),
            ModelFloorTableTask(label="R2", text="second", state="not-started"),
        ),
    )


def blocked(minutes_ago: float, **cells: Any) -> Any:
    return row(
        "TERMINAL",
        minutes_ago,
        "plan-r8-0605",
        tickets=(EPIC,),
        outcome="blocked",
        reason="premise did not hold: the plan has no task R8",
        **cells,
    )


def test_dead_lane_older_than_the_stall_window_does_not_stop_its_line() -> None:
    line = compute_factory_floor(
        request(plans=(context_plan(),), ledger=(blocked(DEAD_MINUTES),))
    ).lines[0]
    assert not line.andon.flag
    assert line.andon.causes == ()
    # The state that is true: nothing is built and nothing runs on it.
    assert (line.status_pill, line.percent_built) == ("queued", 0)


def test_dead_lane_closed_by_the_runner_with_a_later_status_row_stops_nothing() -> None:
    # The live ledger: plan-r8-0605's TERMINAL at 03:53:08Z is followed 30 s later by the runner's closeout STATUS.
    closeout = row(
        "STATUS",
        DEAD_MINUTES - 0.5,
        "plan-r8-0605",
        tickets=(EPIC,),
        text="remote lane on h201 ended status=done engine_rc=0",
    )
    line = compute_factory_floor(
        request(plans=(context_plan(),), ledger=(blocked(DEAD_MINUTES), closeout))
    ).lines[0]
    assert line.andon.causes == ()
    assert line.status_pill == "queued"


def test_dead_lane_that_claimed_again_after_its_terminal_is_not_over() -> None:
    again = row("CLAIM", DEAD_MINUTES - 5, "plan-r8-0605", tickets=(EPIC,))
    line = compute_factory_floor(
        request(plans=(context_plan(),), ledger=(blocked(DEAD_MINUTES), again))
    ).lines[0]
    assert line.status_pill == "stopped"


def test_dead_lane_inside_the_stall_window_still_stops_its_line() -> None:
    window = config().stall_minutes
    line = compute_factory_floor(
        request(plans=(context_plan(),), ledger=(blocked(window - 1),))
    ).lines[0]
    assert line.status_pill == "stopped"
    assert "lane plan-r8-0605 ended blocked" in line.andon.causes[0]


def test_dead_lane_ends_its_stop_at_the_stall_window_itself() -> None:
    window = config().stall_minutes
    line = compute_factory_floor(
        request(plans=(context_plan(),), ledger=(blocked(window),))
    ).lines[0]
    assert line.andon.causes == ()
