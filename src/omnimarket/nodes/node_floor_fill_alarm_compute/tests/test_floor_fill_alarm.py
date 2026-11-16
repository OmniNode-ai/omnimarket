# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill alarm rule: two idle runs above five free slots alarm with the cause; a late or missing CLAIM alarms.

Failing controls: five free slots, one run that sent a lane, a single run and a CLAIM at ten minutes each write no row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_alarm_compute.handlers.handler_floor_fill_alarm import (
    HandlerFloorFillAlarm,
)
from omnimarket.nodes.node_floor_fill_alarm_compute.models import (
    EnumFloorFillAlarmKind,
    ModelFloorFillAlarmRequest,
    ModelFloorFillAlarmRow,
    ModelFloorFillRun,
    ModelFloorFillSentLane,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
CAUSE = "UNKNOWN pool-unread:unbound:pool"
KEY = "floor:alpha:a1"
LANE = "floor-alpha-a1-0001-a1"


def _run(
    minute: int, free: int, sent: int = 0, cause: str = CAUSE
) -> ModelFloorFillRun:
    return ModelFloorFillRun(
        observed_at=T0 + timedelta(minutes=minute),
        free_slots=free,
        lanes_sent=sent,
        cause=cause,
    )


def _alarms(
    runs: tuple[ModelFloorFillRun, ...] = (),
    lanes: tuple[ModelFloorFillSentLane, ...] = (),
    now: datetime = T0 + timedelta(minutes=30),
) -> tuple[ModelFloorFillAlarmRow, ...]:
    return (
        HandlerFloorFillAlarm()
        .handle(ModelFloorFillAlarmRequest(now=now, runs=runs, sent_lanes=lanes))
        .alarms
    )


def _lane(claim_minute: int | None) -> ModelFloorFillSentLane:
    return ModelFloorFillSentLane(
        lane=LANE,
        key=KEY,
        sent_at=T0,
        claim_at=None if claim_minute is None else T0 + timedelta(minutes=claim_minute),
    )


def test_floor_fill_alarm_idle_two_runs_above_five_zero_sent() -> None:
    alarms = _alarms((_run(0, 25, cause="older"), _run(5, 20)))
    assert len(alarms) == 1
    assert alarms[0].kind is EnumFloorFillAlarmKind.IDLE
    assert alarms[0].cause == CAUSE
    assert "free 25,20" in alarms[0].row_text
    assert CAUSE in alarms[0].row_text


def test_floor_fill_alarm_none_when_slots_five_or_fewer() -> None:
    assert _alarms((_run(0, 6), _run(5, 5))) == ()
    assert _alarms((_run(0, 5), _run(5, 5))) == ()


def test_floor_fill_alarm_none_when_any_run_sent() -> None:
    assert _alarms((_run(0, 25), _run(5, 25, sent=1))) == ()
    assert _alarms((_run(0, 25, sent=3), _run(5, 25))) == ()


def test_floor_fill_alarm_none_with_one_run() -> None:
    assert _alarms((_run(0, 25),)) == ()


def test_floor_fill_alarm_uses_last_two_runs_by_time() -> None:
    alarms = _alarms((_run(10, 30), _run(0, 30, sent=2), _run(5, 30)))
    assert [a.kind for a in alarms] == [EnumFloorFillAlarmKind.IDLE]
    assert "free 30,30" in alarms[0].row_text


def test_floor_fill_alarm_idle_without_cause_says_so() -> None:
    alarms = _alarms((_run(0, 9, cause=""), _run(5, 9, cause="")))
    assert alarms[0].cause == "no cause recorded"


def test_floor_fill_alarm_unclaimed_after_ten_minutes() -> None:
    alarms = _alarms(lanes=(_lane(None),), now=T0 + timedelta(minutes=11))
    assert len(alarms) == 1
    assert alarms[0].kind is EnumFloorFillAlarmKind.UNCLAIMED
    assert alarms[0].subject == LANE
    assert f"no CLAIM key={KEY} 11 minutes" in alarms[0].cause


def test_floor_fill_alarm_claim_at_ten_minutes_is_on_time() -> None:
    assert _alarms(lanes=(_lane(10),)) == ()


def test_floor_fill_alarm_late_claim_alarms() -> None:
    alarms = _alarms(lanes=(_lane(11),))
    assert len(alarms) == 1
    assert f"CLAIM key={KEY} arrived 11 minutes" in alarms[0].cause


def test_floor_fill_alarm_no_claim_yet_inside_deadline() -> None:
    assert _alarms(lanes=(_lane(None),), now=T0 + timedelta(minutes=9)) == ()
    assert _alarms(lanes=(_lane(None),), now=T0 + timedelta(minutes=10)) == ()


def test_floor_fill_alarm_idle_row_comes_before_lane_rows() -> None:
    alarms = _alarms(
        (_run(0, 25), _run(5, 25)), (_lane(None),), now=T0 + timedelta(minutes=20)
    )
    assert [a.kind for a in alarms] == [
        EnumFloorFillAlarmKind.IDLE,
        EnumFloorFillAlarmKind.UNCLAIMED,
    ]


def test_floor_fill_alarm_naive_datetime_refused() -> None:
    with pytest.raises(ValidationError):
        ModelFloorFillRun(
            observed_at=datetime(2026, 11, 16, 15, 0), free_slots=9, lanes_sent=0
        )


def test_floor_fill_alarm_not_running_when_no_run_is_recent() -> None:
    request = ModelFloorFillAlarmRequest(
        now=T0 + timedelta(minutes=60),
        runs=(_run(0, 25, sent=1),),
        max_run_age_minutes=15,
    )
    alarms = HandlerFloorFillAlarm().handle(request).alarms
    assert [a.kind for a in alarms] == [EnumFloorFillAlarmKind.NOT_RUNNING]
    assert "no floor fill run in the last 15 minutes" in alarms[0].cause


def test_floor_fill_alarm_not_running_with_no_run_at_all() -> None:
    request = ModelFloorFillAlarmRequest(now=T0, runs=(), max_run_age_minutes=15)
    alarms = HandlerFloorFillAlarm().handle(request).alarms
    assert alarms[0].kind is EnumFloorFillAlarmKind.NOT_RUNNING
    assert "no run read" in alarms[0].cause


def test_floor_fill_alarm_not_running_off_by_default_and_quiet_when_recent() -> None:
    assert _alarms((), now=T0) == ()
    request = ModelFloorFillAlarmRequest(
        now=T0 + timedelta(minutes=10),
        runs=(_run(0, 25, sent=1),),
        max_run_age_minutes=15,
    )
    assert HandlerFloorFillAlarm().handle(request).alarms == ()
