# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24177 AC2: each model runs on its own cadence, never early, and a missed window is not made up.

The clock is injected: every tick carries its own ``now``. A local model runs at days 0 and 7,
not 6.9; the hosted model at days 0 and 28, not 7 or 27.9; a jump from day 1 to day 15 gives the
local model one run, window 2's.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import pytest

from omnimarket.nodes.node_eval_tracking_schedule_compute.handlers import (
    HandlerEvalTrackingSchedule,
)
from tests.nodes.node_eval_tracking_schedule_compute.support import (
    DAY,
    EPOCH,
    GLM,
    LOCAL,
    make_overlay,
    tick_at,
    turns,
)

pytestmark = pytest.mark.unit

HOURS = dt.timedelta(hours=1)


def named_runs(
    handler: HandlerEvalTrackingSchedule,
    arm: int,
    start: dt.datetime,
    end: dt.datetime,
) -> list[UUID]:
    """The run ids this arm's turns in [start, end) name, in order."""
    out: list[UUID] = []
    for at in turns(arm, start, end):
        request = handler.handle(tick_at(at))
        assert request is not None
        out.append(request.correlation_id)
    return out


def test_a_local_model_runs_at_day_0_and_day_7_and_not_at_day_6_9() -> None:
    handler = HandlerEvalTrackingSchedule(make_overlay())
    window_0 = set(named_runs(handler, LOCAL, EPOCH, EPOCH + 7 * DAY))
    assert len(window_0) == 1, "every tick of days 0 to 7 names the one run of window 0"
    at_6_9 = set(
        named_runs(handler, LOCAL, EPOCH + 6.9 * DAY, EPOCH + 6.9 * DAY + HOURS)
    )
    assert at_6_9 == window_0, "day 6.9 starts nothing new"
    window_1 = set(named_runs(handler, LOCAL, EPOCH + 7 * DAY, EPOCH + 14 * DAY))
    assert len(window_1) == 1
    assert window_1.isdisjoint(window_0), "day 7 is the next run"


def test_the_first_tick_of_day_7_is_the_first_tick_of_the_next_run() -> None:
    handler = HandlerEvalTrackingSchedule(make_overlay())
    (before,) = {
        r.correlation_id
        for at in turns(
            LOCAL, EPOCH + 7 * DAY - 10 * dt.timedelta(minutes=1), EPOCH + 7 * DAY
        )
        if (r := handler.handle(tick_at(at))) is not None
    }
    first_at = next(turns(LOCAL, EPOCH + 7 * DAY, EPOCH + 8 * DAY))
    request = handler.handle(tick_at(first_at))
    assert request is not None
    assert request.correlation_id != before


def test_the_hosted_model_runs_at_day_0_and_day_28_and_not_at_day_7_or_27_9() -> None:
    handler = HandlerEvalTrackingSchedule(make_overlay())
    window_0 = set(named_runs(handler, GLM, EPOCH, EPOCH + 28 * DAY))
    assert len(window_0) == 1, "day 7 and day 27.9 name the run of window 0"
    window_1 = set(named_runs(handler, GLM, EPOCH + 28 * DAY, EPOCH + 29 * DAY))
    assert len(window_1) == 1
    assert window_1.isdisjoint(window_0)


def test_a_jump_from_day_1_to_day_15_gives_the_local_model_one_run_windows_2() -> None:
    handler = HandlerEvalTrackingSchedule(make_overlay())
    day_1 = named_runs(handler, LOCAL, EPOCH + 1 * DAY, EPOCH + 1 * DAY + HOURS)
    day_15 = named_runs(handler, LOCAL, EPOCH + 15 * DAY, EPOCH + 15 * DAY + HOURS)
    window_2 = named_runs(handler, LOCAL, EPOCH + 14 * DAY, EPOCH + 21 * DAY)
    assert len(set(day_1)) == 1
    assert len(set(day_15)) == 1
    assert set(day_15) == set(window_2), "day 15 is window 2's run, not window 1's"
    window_1 = set(named_runs(handler, LOCAL, EPOCH + 7 * DAY, EPOCH + 14 * DAY))
    assert not window_1 & (set(day_1) | set(day_15)), "window 1 is never made up"


def test_each_model_keeps_its_own_cadence_on_the_same_clock() -> None:
    handler = HandlerEvalTrackingSchedule(make_overlay())
    local = {
        i: set(
            named_runs(handler, LOCAL, EPOCH + i * 7 * DAY, EPOCH + (i + 1) * 7 * DAY)
        )
        for i in range(4)
    }
    glm = set(named_runs(handler, GLM, EPOCH, EPOCH + 28 * DAY))
    assert {len(runs) for runs in local.values()} == {1}
    assert len(set().union(*local.values())) == 4, "four local runs in 28 days"
    assert len(glm) == 1, "one hosted run in 28 days"
    assert glm.isdisjoint(set().union(*local.values()))


def test_a_tick_before_the_epoch_names_no_run() -> None:
    handler = HandlerEvalTrackingSchedule(make_overlay())
    for arm in (LOCAL, GLM):
        for at in turns(arm, EPOCH - 3 * HOURS, EPOCH):
            assert handler.handle(tick_at(at)) is None


def test_a_tick_that_is_nobodys_turn_names_no_run() -> None:
    handler = HandlerEvalTrackingSchedule(make_overlay())
    at = next(turns(LOCAL, EPOCH + DAY, EPOCH + 2 * DAY))
    assert handler.handle(tick_at(at + dt.timedelta(seconds=1))) is None


def test_a_cadence_value_is_a_setting_that_changes_the_window() -> None:
    weekly = HandlerEvalTrackingSchedule(make_overlay(local_days=7))
    daily = HandlerEvalTrackingSchedule(make_overlay(local_days=1))
    assert len(set(named_runs(weekly, LOCAL, EPOCH, EPOCH + 3 * DAY))) == 1
    assert len(set(named_runs(daily, LOCAL, EPOCH, EPOCH + 3 * DAY))) == 3
    start = next(turns(LOCAL, EPOCH, EPOCH + DAY))
    first_weekly = weekly.handle(tick_at(start))
    first_daily = daily.handle(tick_at(start))
    assert first_weekly is not None
    assert first_daily is not None
    assert first_weekly.correlation_id != first_daily.correlation_id, (
        "a cadence change must not reuse a run id of the old windows"
    )


def test_the_overlay_has_no_default_cadence() -> None:
    document = make_overlay().model_dump(mode="json")
    del document["local_days"]
    with pytest.raises(ValueError, match="local_days"):
        type(make_overlay()).model_validate(document)
