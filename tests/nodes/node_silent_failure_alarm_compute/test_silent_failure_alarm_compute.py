"""The silent-failure alarm decisions: placement share and tick liveness, pure and clockless.

The two measured silent failures of 2026-10-09 are the cases: lab-fill dispatched lanes the
runner could not place for hours, and the session ticks were not recreated after a restart.
Each decision takes the facts a caller read and returns an alarm verdict with its FRICTION
cells, so the tests state the facts and read the verdict.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_silent_failure_alarm_compute.handlers import (
    handler_placement_share_alarm as placement,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.handlers import (
    handler_quiet_host_alarm as quiet,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.handlers import (
    handler_throughput_results_alarm as results,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.handlers import (
    handler_tick_liveness_alarm as liveness,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.models import (
    ModelHostActivity,
    ModelLaneRefusal,
    ModelPlacementRun,
    ModelPlacementShareRequest,
    ModelQuietHostRequest,
    ModelThroughputResultsRequest,
    ModelTickLivenessRequest,
)

pytestmark = pytest.mark.unit


_TICKET_CELL = re.compile(r"(?:^|\|\s)(?:ticket|existing)=OMN-\d+")
_LANE_CELL = re.compile(r"(?:^|\|\s)lane=\S+")
_COST_CELL = re.compile(r"(?:^|\|)\s*costs?\s*[:=]\s*[^|\n]*?\d", re.IGNORECASE)
_COST_DURATION = re.compile(
    r"(?<![\w-])~?\s*<?\s*\d+(?:\.\d+)?\s*(?:lane-)?(?:hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)


def _friction_row_reason(line: str) -> str | None:
    """The three obligations of a FRICTION row: a lane, a ticket and a cost estimate."""
    missing = [
        name
        for name, pattern in (
            ("lane", _LANE_CELL),
            ("ticket", _TICKET_CELL),
            ("cost", _COST_CELL),
        )
        if not pattern.search(line)
    ]
    if "cost" in missing and _COST_DURATION.search(line):
        missing.remove("cost")
    return f"FRICTION row is missing {', '.join(missing)}" if missing else None


NOW = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)


def _run(key: str, dispatched: int, placed: int, minutes_ago: int) -> ModelPlacementRun:
    return ModelPlacementRun(
        run_key=key,
        stamp=NOW - timedelta(minutes=minutes_ago),
        dispatched=dispatched,
        placed=placed,
    )


def _refusals(key: str, status: str, count: int) -> tuple[ModelLaneRefusal, ...]:
    return tuple(
        ModelLaneRefusal(run_key=key, lane=f"lab-fill-lane{i:03d}-{key}", status=status)
        for i in range(count)
    )


def _share(
    runs: tuple[ModelPlacementRun, ...], refusals: tuple[ModelLaneRefusal, ...] = ()
) -> placement.ModelPlacementShareResult:
    request = ModelPlacementShareRequest(now=NOW, runs=runs, refusals=refusals)
    return placement.HandlerPlacementShareAlarm().handle(request)


def test_two_consecutive_runs_under_half_alarm_and_name_the_refusal_reason() -> None:
    result = _share(
        (_run("10090110", 2, 0, 80), _run("10090130", 2, 0, 60)),
        _refusals("10090110", "repo-unresolved", 2)
        + _refusals("10090130", "repo-unresolved", 2),
    )
    assert result.alarm
    assert "placement-share" in result.kinds
    assert result.reason == "repo-unresolved"
    assert (result.dispatched, result.placed) == (4, 0)
    assert "reason=repo-unresolved" in result.friction_cells
    assert "repo-unresolved" in result.alert_text


def test_one_bad_run_between_good_ones_is_quiet() -> None:
    assert not _share(
        (_run("a", 10, 10, 100), _run("b", 10, 1, 60), _run("c", 5, 5, 20))
    ).alarm


def test_exactly_half_placed_is_not_under_half() -> None:
    assert not _share((_run("a", 10, 5, 60), _run("b", 4, 2, 20))).alarm


def test_zero_dispatch_runs_neither_count_nor_break_the_pair() -> None:
    runs = (
        _run("a", 12, 5, 90),
        _run("b", 0, 0, 70),
        _run("c", 0, 0, 50),
        _run("d", 2, 0, 30),
    )
    assert _share(runs).alarm


def test_only_the_newest_pair_decides() -> None:
    runs = (
        _run("a", 10, 0, 90),
        _run("b", 10, 0, 70),
        _run("c", 10, 10, 50),
        _run("d", 4, 4, 30),
    )
    assert not _share(runs).alarm


def test_a_single_acting_run_is_not_two_consecutive_runs() -> None:
    assert not _share((_run("a", 11, 0, 20),)).alarm


def test_runs_after_now_are_not_yet_known() -> None:
    future = ModelPlacementRun(
        run_key="z", stamp=NOW + timedelta(minutes=5), dispatched=9, placed=0
    )
    assert not _share((_run("a", 9, 0, 30), future)).alarm


def test_lanes_with_no_refusal_receipt_are_reported_as_unreported_not_invented() -> (
    None
):
    result = _share((_run("a", 10, 1, 60), _run("b", 10, 2, 20)))
    assert result.alarm
    assert result.reason == "no-refusal-reported"


def test_the_same_refusal_for_most_of_one_run_alarms_even_when_placement_is_not_in_a_pair() -> (
    None
):
    result = _share((_run("b", 5, 3, 20),), _refusals("b", "no-host", 3))
    assert result.alarm
    assert result.kinds == ("refusal-reason",)
    assert result.reason == "no-host"


def test_a_minority_refusal_is_quiet() -> None:
    result = _share((_run("b", 5, 3, 20),), _refusals("b", "no-host", 2))
    assert not result.alarm


def test_two_lanes_are_the_least_evidence_for_a_majority() -> None:
    assert not _share(
        (_run("b", 1, 0, 20),), _refusals("b", "repo-unresolved", 1)
    ).alarm


def test_the_most_common_reason_across_the_window_is_named() -> None:
    result = _share(
        (_run("a", 6, 0, 60), _run("b", 6, 0, 20)),
        _refusals("a", "no-host", 2) + _refusals("b", "repo-unresolved", 5),
    )
    assert result.reason == "repo-unresolved"


def test_quiet_result_carries_no_rows() -> None:
    result = _share((_run("a", 3, 3, 20),))
    assert not result.alarm
    assert result.friction_cells == ""
    assert result.alert_text == ""
    assert result.reason == ""


def test_the_friction_cells_pass_the_friction_guard() -> None:
    result = _share(
        (_run("a", 11, 0, 60), _run("b", 11, 0, 20)),
        _refusals("b", "repo-unresolved", 11),
    )
    row = f"2026-10-09T06:00:00Z | {result.friction_cells}"
    assert _friction_row_reason(row) is None


def _live(
    *,
    trend_minutes: float | None,
    controller: bool | None = True,
) -> liveness.ModelTickLivenessResult:
    def at(minutes: float | None) -> datetime | None:
        return None if minutes is None else NOW - timedelta(minutes=minutes)

    request = ModelTickLivenessRequest(
        now=NOW,
        trend_at=at(trend_minutes),
        controller_live=controller,
    )
    return liveness.HandlerTickLivenessAlarm().handle(request)


def test_a_trend_five_hours_old_with_a_live_controller_alarms_with_the_fix() -> None:
    result = _live(trend_minutes=339)
    assert result.alarm
    assert result.state == "STALE"
    assert len(result.stale) == 1
    assert "trend" in result.stale[0]
    assert "/omni:session-start" not in result.fix
    assert "queue-status-tick" in result.fix
    assert "5h39m" in result.alert_text


def test_the_heartbeat_is_not_a_liveness_source() -> None:
    """OMN-19998: a launchd tick refreshed the heartbeat while no session ran; it proved nothing."""
    assert "heartbeat_at" not in ModelTickLivenessRequest.model_fields


def test_exactly_forty_minutes_is_not_older_than_forty() -> None:
    assert not _live(trend_minutes=40).alarm


def test_fresh_ticks_are_quiet() -> None:
    result = _live(trend_minutes=12)
    assert not result.alarm
    assert result.state == "LIVE"
    assert result.stale == ()


def test_a_missing_source_is_stale_not_fresh() -> None:
    result = _live(trend_minutes=None)
    assert result.alarm
    assert "absent" in result.stale[0]


def test_no_alarm_while_the_controller_is_not_live() -> None:
    result = _live(trend_minutes=339, controller=False)
    assert not result.alarm
    assert result.state == "SKIPPED"


def test_an_unreadable_controller_is_unknown_never_live() -> None:
    result = _live(trend_minutes=339, controller=None)
    assert not result.alarm
    assert result.state == "UNKNOWN"


def test_a_stamp_in_the_future_is_stale() -> None:
    result = _live(trend_minutes=-30)
    assert result.alarm
    assert "future" in result.stale[0]


def test_liveness_friction_cells_pass_the_friction_guard() -> None:
    result = _live(trend_minutes=339)
    row = f"2026-10-09T06:00:00Z | {result.friction_cells}"
    assert _friction_row_reason(row) is None


def test_an_older_runs_refusals_do_not_alarm_once_a_later_run_places() -> None:
    result = _share(
        (_run("a", 10, 0, 60), _run("b", 11, 11, 20)),
        _refusals("a", "repo-unresolved", 10),
    )
    assert not result.alarm


# ---- a pool host that ran lanes and sent no hook event (OMN-18603) -----------------------------


def _host(
    host: str, last_event_minutes_ago: float | None, *lane_minutes_ago: float
) -> ModelHostActivity:
    return ModelHostActivity(
        host=host,
        last_event_at=None
        if last_event_minutes_ago is None
        else NOW - timedelta(minutes=last_event_minutes_ago),
        lane_stamps=tuple(
            NOW - timedelta(minutes=m) for m in sorted(lane_minutes_ago, reverse=True)
        ),
    )


def _quiet(
    *hosts: ModelHostActivity, readable: bool = True
) -> quiet.ModelQuietHostResult:
    request = ModelQuietHostRequest(now=NOW, hosts=hosts, events_readable=readable)
    return quiet.HandlerQuietHostAlarm().handle(request)


def test_a_host_with_lanes_in_the_last_hour_and_no_event_for_half_an_hour_alarms() -> (
    None
):
    result = _quiet(_host("h202", 6 * 24 * 60, 50, 40, 12), _host("h201", 1, 50, 5))
    assert result.alarm
    assert result.state == "QUIET"
    assert result.quiet_hosts == ("h202",)
    assert "h202" in result.alert_text
    assert "h201" not in result.alert_text
    assert "class=quiet-host" in result.friction_cells
    assert "hosts=h202" in result.friction_cells
    assert "install-hook-emit-drainer.sh --status" in result.fix


def test_a_host_that_never_sent_an_event_is_quiet_once_its_lanes_have_run_half_an_hour() -> (
    None
):
    assert _quiet(_host("h202", None, 45, 20)).quiet_hosts == ("h202",)


def test_a_recent_event_keeps_the_host_live() -> None:
    result = _quiet(_host("h202", 29, 50, 40, 5))
    assert not result.alarm
    assert result.state == "LIVE"


def test_a_host_whose_last_lane_ended_with_its_last_event_is_idle_not_quiet() -> None:
    # The final row of the last lane lands seconds after the final hook event; nothing ran since.
    assert not _quiet(_host("h202", 50.5, 58, 50)).alarm


def test_a_lane_that_started_minutes_ago_is_not_yet_a_quiet_host() -> None:
    assert not _quiet(_host("h202", 300, 5)).alarm


def test_a_host_with_no_lane_row_in_the_last_hour_is_not_judged() -> None:
    assert not _quiet(_host("h202", 600, 90, 75)).alarm


def test_unreadable_events_are_unknown_never_live_or_quiet() -> None:
    result = _quiet(_host("h202", 600, 50, 40), readable=False)
    assert not result.alarm
    assert result.state == "UNKNOWN"


def test_quiet_host_friction_cells_pass_the_friction_guard() -> None:
    cells = _quiet(_host("h202", 6000, 50, 40)).friction_cells
    assert _friction_row_reason(f"2026-10-09T06:00:00Z | {cells}") is None


def test_event_hostnames_resolve_to_the_pool_host_ids() -> None:
    aliases = {"h1": "alpha", "h2": "beta-gamma"}
    assert quiet.host_id("alpha", aliases) == "h1"
    assert quiet.host_id("Alpha.local", aliases) == "h1"
    assert quiet.host_id("Beta-Gamma.local", aliases) == "h2"
    assert quiet.host_id("docker-desktop", aliases) is None


# OMN-19998: the alarm judges what the factory produced, merges and lanes placed in the last hour,
# not a heartbeat a timer can refresh while nothing lands.
def _results(
    *,
    merges: int | None = 6,
    open_prs: int | None = 148,
    placed: int | None = 3,
) -> results.ModelThroughputResultsResult:
    return results.HandlerThroughputResultsAlarm().handle(
        ModelThroughputResultsRequest(
            now=NOW,
            merges_last_hour=merges,
            open_prs=open_prs,
            lanes_placed_last_hour=placed,
        )
    )


def test_results_merges_and_placements_at_their_bounds_are_quiet() -> None:
    result = _results(merges=5, placed=1)
    assert not result.alarm
    assert result.state == "PRODUCING"
    assert result.short == ()


def test_results_merges_under_the_hourly_bound_with_prs_open_alarm() -> None:
    result = _results(merges=0)
    assert result.alarm
    assert result.state == "SHORT"
    (short,) = result.short
    assert "0 merges" in short
    assert "148 open" in short
    assert "0 merges" in result.alert_text


def test_results_no_lane_placed_in_the_hour_alarms() -> None:
    result = _results(placed=0)
    assert result.alarm
    assert result.state == "SHORT"
    (short,) = result.short
    assert "0 lanes placed" in short


def test_results_both_short_name_both() -> None:
    result = _results(merges=1, placed=0)
    assert result.alarm
    assert len(result.short) == 2


def test_results_merges_are_not_judged_with_nothing_open() -> None:
    assert not _results(merges=0, open_prs=0).alarm


def test_results_an_unreadable_source_is_raised_never_a_pass() -> None:
    result = _results(merges=None, open_prs=None)
    assert result.alarm
    assert result.state == "UNKNOWN"
    assert "unreadable" in result.short[0]
    placed = _results(placed=None)
    assert placed.alarm
    assert placed.state == "UNKNOWN"


def test_results_a_bound_of_zero_turns_that_measure_off() -> None:
    request = ModelThroughputResultsRequest(
        now=NOW,
        merges_last_hour=None,
        open_prs=None,
        lanes_placed_last_hour=None,
        min_merges_per_hour=0,
        min_lanes_placed_per_hour=0,
    )
    result = results.HandlerThroughputResultsAlarm().handle(request)
    assert not result.alarm
    assert result.state == "PRODUCING"


def test_results_friction_cells_pass_the_friction_guard() -> None:
    result = _results(merges=0, placed=0)
    row = f"2026-10-09T06:00:00Z | {result.friction_cells}"
    assert _friction_row_reason(row) is None
    assert "class=throughput-results" in result.friction_cells


def test_the_friction_check_names_what_a_row_lacks() -> None:
    assert _friction_row_reason("2026-10-09T06:00:00Z | FRICTION | actor=x | text") == (
        "FRICTION row is missing lane, ticket, cost"
    )
