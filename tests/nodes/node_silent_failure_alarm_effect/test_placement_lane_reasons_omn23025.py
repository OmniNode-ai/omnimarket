# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The placement alarm reads each lane of a lab-fill run from the ledger's own rows (OMN-23025).

On 2026-11-08 the alarm read run 0810 as 6 of 18 placed and run 0830 as 3 of 15 placed and named no
reason (``no-refusal-reported``). The fill writes its STATUS row while its detached send jobs are
still building: 24 lanes read ``pending`` there and every one got its CLAIM row one to ten minutes
later. The lanes the fill skipped named their reason in the same row, and the runner's lane-end
report is a STATUS row since OMN-21822, which the alarm's reader (MSG rows only) never saw.

Each reading below has a control: the same ledger with the one fact that makes the reading true
removed (no CLAIM row, no lane table, a lane-end row that exited 0), which must read the other way.
That is the named injected break; the unfixed reader fails the readings marked RED.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.silent_failure_alarm import (
    ModelDrainerObservation,
    ModelLaneRefusal,
    ModelPlacementRun,
    ModelPlacementShareRequest,
)
from omnimarket.nodes.node_silent_failure_alarm_compute import (
    HandlerPlacementShareAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.lab_fill_lane_reading import (
    parse_lanes,
    status_slug,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelSilentFailureAlarmOptions,
    ModelSilentFailureAlarmRequest,
)
from tests.nodes.node_silent_failure_alarm_compute.test_golden_chain_silent_failure_alarm_compute import (
    RECORDED,
    Watch,
    world,
)

pytestmark = pytest.mark.unit

T0810 = datetime(2026, 11, 8, 8, 25, 0, tzinfo=UTC)
T0830 = datetime(2026, 11, 8, 8, 44, 38, tzinfo=UTC)
ALARM_AT = datetime(2026, 11, 8, 8, 45, 44, tzinfo=UTC)
LATER = datetime(2026, 11, 8, 9, 10, 0, tzinfo=UTC)


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _lane(ticket: str, key: str) -> str:
    return f"lab-fill-omn{ticket}-{key}"


def _fill_row(
    at: datetime, run: str, dispatched: int, placed: int, lanes: list[str]
) -> str:
    return (
        f"{_stamp(at)} | STATUS | lane=lab-fill | ticket=OMN-22478 | actor=launchd:lab-fill | run={run} | "
        f"dispatched={dispatched} | placed={placed} | lanes={','.join(lanes)} | "
        "states=preparing:2 | refusals=none | fill row"
    )


def _claim(at: datetime, lane: str, host: str = "h201") -> str:
    return f"{_stamp(at)} | CLAIM | lane={lane} | ticket=OMN-22478 | host={host} | placed by the runner"


def _lane_end(at: datetime, lane: str, status: str, exit_code: int) -> str:
    return (
        f"{_stamp(at)} | STATUS | lane={lane} | to=orchestrator-31d9 | ticket=OMN-22478 | status={status} | "
        f"host=none | exit={exit_code} | run=rlane-{lane} | detached lane ended"
    )


def _append(options: ModelSilentFailureAlarmOptions, *rows: str) -> None:
    assert options.ledger is not None
    with options.ledger.open("a") as handle:
        handle.write("".join(f"{row}\n" for row in rows))


def _tick(options: ModelSilentFailureAlarmOptions, at: datetime) -> tuple[str, ...]:
    """The placement FRICTION rows one tick at ``at`` writes."""
    watches = {
        host: Watch(
            ModelDrainerObservation(job_state="not-running", status_readable=False)
        )
        for host in options.host_aliases
    }
    result = HandlerSilentFailureAlarm(options, host_watches=watches).handle(
        ModelSilentFailureAlarmRequest(now=at)
    )
    assert result.exit_code == 0, result.warnings
    return tuple(
        r
        for r in result.rows
        if "class=placement-share" in r or "class=refusal-reason" in r
    )


def _incident(options: ModelSilentFailureAlarmOptions, *, claims: bool) -> list[str]:
    """Two runs as 2026-11-08 wrote them: a few lanes placed, the rest pending in the fill's row."""
    a_pending = [_lane(f"2{n}", "11080810") for n in range(30, 36)]
    b_pending = [_lane(f"2{n}", "11080830") for n in range(40, 45)]
    a_placed = _lane("2201", "11080810")
    b_placed = _lane("2202", "11080830")
    _append(
        options,
        _fill_row(
            T0810,
            "2026-11-08T0810Z",
            7,
            1,
            [f"{a_placed}@h051:placed", *(f"{n}@h053:pending" for n in a_pending)],
        ),
        _fill_row(
            T0830,
            "2026-11-08T0830Z",
            6,
            1,
            [f"{b_placed}@h051:placed", *(f"{n}@h053:pending" for n in b_pending)],
        ),
    )
    if claims:
        for i, name in enumerate([*a_pending, *b_pending]):
            _append(options, _claim(T0810 + timedelta(minutes=1 + i), name))
    return [*a_pending, *b_pending]


def test_golden_pending_lanes_that_got_a_claim_are_placed_and_raise_nothing(
    tmp_path: Path,
) -> None:
    """RED before the fix: 1 of 7 and 1 of 6 read under half twice in a row and alarmed."""
    options = world(tmp_path)
    _incident(options, claims=True)
    assert _tick(options, LATER) == ()


def test_control_the_same_pending_lanes_with_no_claim_past_the_cap_alarm_and_name_each_lane(
    tmp_path: Path,
) -> None:
    options = world(tmp_path)
    lanes = _incident(options, claims=False)
    rows = _tick(options, LATER)
    assert len(rows) == 1, rows
    row = rows[0]
    assert (
        "class=placement-share-refusal-reason" in row or "class=placement-share" in row
    )
    assert "reason=send-job-no-claim" in row
    assert "no-refusal-reported" not in row
    assert "dispatched=13 | placed=2" in row
    for lane in lanes:
        assert f"{lane}:send-job-no-claim" in row


def test_pending_lanes_inside_the_send_cap_are_in_flight_and_judged_neither_way(
    tmp_path: Path,
) -> None:
    """The fire has just written its row: its send jobs have had under the cap, so nothing alarms."""
    options = world(tmp_path)
    _incident(options, claims=False)
    assert _tick(options, T0830 + timedelta(minutes=2)) == ()


def test_error_chain_lanes_the_fill_skipped_are_named_by_the_fills_own_reason(
    tmp_path: Path,
) -> None:
    """RED before the fix: 22 of 41 lanes skipped on ``brief-rules-unreadable`` read as no reason at all."""
    options = world(tmp_path)
    skipped = [_lane(f"3{n}", "11080810") for n in range(10, 20)]
    placed = [_lane(f"3{n}", "11080810") for n in range(20, 22)]
    _append(
        options,
        _fill_row(
            T0810,
            "2026-11-08T0810Z",
            2,
            2,
            [
                *(f"{n}@h051:skipped:brief-rules-unreadable" for n in skipped),
                *(f"{n}@h051:placed" for n in placed),
            ],
        ),
    )
    rows = _tick(options, LATER)
    assert len(rows) == 1, rows
    assert "class=refusal-reason" in rows[0]
    assert "reason=brief-rules-unreadable" in rows[0]
    for lane in skipped:
        assert f"{lane}:brief-rules-unreadable" in rows[0]


def test_control_a_minority_of_skipped_lanes_stays_quiet(tmp_path: Path) -> None:
    options = world(tmp_path)
    placed = [_lane(f"4{n}", "11080810") for n in range(10, 18)]
    _append(
        options,
        _fill_row(
            T0810,
            "2026-11-08T0810Z",
            8,
            8,
            [
                f"{_lane('4001', '11080810')}@h051:skipped:brief-check-refused",
                *(f"{n}@h051:placed" for n in placed),
            ],
        ),
    )
    assert _tick(options, LATER) == ()


def test_lane_end_status_rows_name_the_runner_refusal_the_alarm_used_to_miss(
    tmp_path: Path,
) -> None:
    """RED before the fix: the runner's lane-end report is a STATUS row, and only MSG rows were read."""
    options = world(tmp_path)
    lanes = [_lane(f"5{n}", "11080810") for n in range(10, 14)]
    _append(
        options,
        _fill_row(
            T0810,
            "2026-11-08T0810Z",
            4,
            0,
            [f"{n}@h053:placed" for n in lanes],
        ),
        *(_lane_end(T0810 + timedelta(minutes=2), n, "no-host", 75) for n in lanes),
    )
    rows = _tick(options, LATER)
    assert len(rows) == 1, rows
    assert "reason=no-host" in rows[0]
    assert all(f"{n}:no-host" in rows[0] for n in lanes)


def test_control_lane_end_rows_that_exited_zero_are_not_refusals(
    tmp_path: Path,
) -> None:
    options = world(tmp_path)
    lanes = [_lane(f"6{n}", "11080810") for n in range(10, 14)]
    _append(
        options,
        _fill_row(
            T0810,
            "2026-11-08T0810Z",
            4,
            4,
            [f"{n}@h053:placed" for n in lanes],
        ),
        *(_lane_end(T0810 + timedelta(minutes=2), n, "done", 0) for n in lanes),
    )
    assert _tick(options, LATER) == ()


def test_the_friction_row_counts_the_lanes_it_does_not_name() -> None:
    refusals = tuple(
        ModelLaneRefusal(run_key="11080810", lane=f"lane-{n:02d}", status="no-host")
        for n in range(5)
    )
    runs = (
        ModelPlacementRun(
            run_key="11080810",
            stamp=ALARM_AT - timedelta(minutes=30),
            dispatched=5,
            placed=0,
        ),
        ModelPlacementRun(
            run_key="11080830",
            stamp=ALARM_AT - timedelta(minutes=10),
            dispatched=5,
            placed=0,
        ),
    )
    result = HandlerPlacementShareAlarm().handle(
        ModelPlacementShareRequest(
            now=ALARM_AT, runs=runs, refusals=refusals, lanes_named=2
        )
    )
    assert result.alarm
    assert (
        "lane-reasons=lane-00:no-host,lane-01:no-host,+3-more" in result.friction_cells
    )


def test_a_run_with_no_lane_refusals_carries_no_lane_reasons_cell() -> None:
    runs = (
        ModelPlacementRun(
            run_key="a", stamp=ALARM_AT - timedelta(minutes=30), dispatched=5, placed=0
        ),
        ModelPlacementRun(
            run_key="b", stamp=ALARM_AT - timedelta(minutes=10), dispatched=5, placed=0
        ),
    )
    result = HandlerPlacementShareAlarm().handle(
        ModelPlacementShareRequest(now=ALARM_AT, runs=runs)
    )
    assert result.alarm
    assert result.reason == "no-refusal-reported"
    assert "lane-reasons" not in result.friction_cells


def test_lane_table_parser_keeps_the_reason_and_drops_a_malformed_entry() -> None:
    lanes = parse_lanes(
        "lab-fill-omn1-11080810@h051:skipped:lane-timeout:18s,junk,lab-fill-omn2-11080810@h053:pending"
    )
    assert [(lane.name, lane.state, lane.reason) for lane in lanes] == [
        ("lab-fill-omn1-11080810", "skipped", "lane-timeout:18s"),
        ("lab-fill-omn2-11080810", "pending", ""),
    ]
    assert status_slug("lane-timeout:18s") == "lane-timeout-18s"
    assert status_slug(":::") == "unknown"
    assert RECORDED  # the shared fixture's hosts are in use by `world`
