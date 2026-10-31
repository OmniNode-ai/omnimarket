# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behaviour of the process-health series: purity, series, unread sources, reset summary, determinism."""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.process_health import (
    EnumReadStatus,
    EnumSourceName,
    EnumVerdict,
    ModelDaySeries,
    ModelMetric,
    ModelProcessHealthResult,
    ModelSourceRead,
)
from omnimarket.nodes.node_process_health_compute.handlers.handler_process_health import (
    HandlerProcessHealth,
)
from omnimarket.nodes.node_process_health_compute.handlers.ledger_view import (
    cost_minutes,
)
from omnimarket.nodes.node_process_health_compute.tests.fixtures import (
    GENERATED_AT,
    at,
    request,
)

NODE_DIR = Path(__file__).resolve().parent.parent


def _run(**overrides: object) -> ModelProcessHealthResult:
    return HandlerProcessHealth().handle(request(**overrides))


def _day(result: ModelProcessHealthResult, date: str) -> ModelDaySeries:
    return next(day for day in result.days if day.date == date)


def _metric(day: ModelDaySeries, key: str) -> ModelMetric:
    return next(m for m in day.window.metrics if m.key == key)


def _breakdown(metric: ModelMetric) -> dict[str, float]:
    return {item.key: item.value for item in metric.breakdown}


@pytest.mark.unit
def test_purity_handlers_read_no_file_network_or_clock() -> None:
    banned = {
        "os",
        "pathlib",
        "subprocess",
        "socket",
        "requests",
        "httpx",
        "time",
        "urllib",
    }
    for path in sorted((NODE_DIR / "handlers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
                assert not names & banned, f"{path.name} imports {names & banned}"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned, (
                    f"{path.name} imports {node.module}"
                )
            if isinstance(node, ast.Call):
                func = node.func
                assert not (isinstance(func, ast.Name) and func.id == "open"), (
                    f"{path.name} calls open()"
                )
                assert not (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"now", "utcnow", "today"}
                ), f"{path.name} reads the clock"


@pytest.mark.unit
def test_series_has_one_entry_per_day_with_every_metric_family() -> None:
    result = _run()
    assert len(result.days) == 14
    assert result.days[-1].date == "2026-10-30"
    assert result.days[-1].partial is True
    assert [d.date for d in result.days if d.is_reset_day] == ["2026-10-29"]
    keys = {m.key for m in _day(result, "2026-10-28").window.metrics}
    for family in (
        "reds.",
        "regressions.",
        "bugs.",
        "validation.",
        "lanes.",
        "merges.",
        "andon.",
        "friction.",
        "delegation.",
    ):
        assert any(key.startswith(family) for key in keys), family


@pytest.mark.unit
def test_series_values_on_the_day_before_the_reset() -> None:
    day = _day(_run(), "2026-10-28")
    assert _metric(day, "reds.hours_red").value == 3.0
    assert _breakdown(_metric(day, "reds.hours_red")) == {"omnimarket": 3.0}
    assert _metric(day, "reds.episodes_opened").value == 1.0
    assert _metric(day, "merges.total").value == 7.0
    assert _breakdown(_metric(day, "merges.product")) == {"omnimarket": 7.0}
    assert _metric(day, "regressions.fixed_forward").value == 1.0
    assert _metric(day, "regressions.reverted").value == 0.0
    assert _metric(day, "validation.refusals").value == 2.0
    assert _breakdown(_metric(day, "validation.refusals")) == {
        "foreground-bash-shape": 1.0,
        "preflight": 1.0,
    }
    assert _metric(day, "validation.preflight_blocks").value == 1.0
    assert _metric(day, "validation.required_check_failures").value == 1.0
    assert _metric(day, "bugs.opened").value == 1.0
    assert _metric(day, "bugs.closed").value == 0.0
    assert _metric(day, "merges.green_to_merged_min").value == 50.0
    assert _metric(day, "merges.controller_to_merged_median_min").value == 60.0


@pytest.mark.unit
def test_lane_terminals_are_deduplicated_and_classed() -> None:
    day = _day(_run(), "2026-10-28")
    assert _metric(day, "lanes.terminals").value == 3.0
    assert _breakdown(_metric(day, "lanes.terminals")) == {
        "blocked": 1.0,
        "done": 1.0,
        "landing-failed": 1.0,
    }
    assert _metric(day, "lanes.share_done").value == pytest.approx(0.333, abs=1e-3)
    assert _metric(day, "lanes.share_landing_failed").value == pytest.approx(
        0.333, abs=1e-3
    )
    assert _metric(day, "delegation.lane_share").value == 0.5
    assert _metric(day, "delegation.landing_share").value == 0.5


@pytest.mark.unit
def test_friction_keeps_lane_stated_reporter_stated_and_capacity_estimates_apart() -> (
    None
):
    result = _run()
    day = _day(result, "2026-10-28")
    assert _metric(day, "friction.lane_stated_minutes").value == 30.0
    assert _metric(day, "friction.reporter_stated_minutes").value == 2.0
    assert _metric(day, "friction.capacity_estimate_minutes").value == 140.0
    assert _breakdown(_metric(day, "friction.capacity_estimate_minutes")) == {
        "lab-idle": 140.0
    }
    assert _metric(day, "friction.measured_lease_minutes").value == 16.0
    assert (
        _metric(_day(result, "2026-10-29"), "friction.lane_stated_minutes").value
        == 60.0
    )


@pytest.mark.unit
def test_andon_hours_run_from_the_hold_to_its_release() -> None:
    result = _run()
    day = _day(result, "2026-10-29")
    assert _metric(day, "andon.stops_started").value == 1.0
    assert _metric(day, "andon.hours_stopped").value == 4.0
    assert _metric(_day(result, "2026-10-28"), "andon.hours_stopped").value == 0.0


@pytest.mark.unit
def test_andon_without_a_release_stands_until_the_generated_at_time() -> None:
    rows = tuple(row for row in request().ledger_rows if row.rtype != "RELEASE")
    result = _run(ledger_rows=rows)
    assert _metric(
        _day(result, "2026-10-29"), "andon.hours_stopped"
    ).value == pytest.approx(18.5)
    assert _metric(_day(result, "2026-10-30"), "andon.hours_stopped").value == 12.0


@pytest.mark.unit
def test_regressions_count_relapses_reverts_and_skip_umbrella_tickets() -> None:
    result = _run()
    assert (
        _metric(_day(result, "2026-10-29"), "regressions.cause_relapses").value == 1.0
    )
    assert _metric(_day(result, "2026-10-29"), "regressions.reverted").value == 1.0
    # OMN-100 is a fix-forward; the five OMN-999 fixes name an umbrella ticket and are not counted.
    assert _metric(_day(result, "2026-10-28"), "regressions.fixed_forward").value == 1.0


@pytest.mark.unit
def test_red_episode_still_open_runs_to_the_generated_at_time() -> None:
    result = _run()
    assert _metric(_day(result, "2026-10-29"), "reds.hours_red").value == 2.0
    assert _metric(_day(result, "2026-10-30"), "reds.hours_red").value == 12.0


@pytest.mark.unit
def test_unread_source_is_reported_unread_never_zero() -> None:
    sources = tuple(
        ModelSourceRead(
            name=s.name,
            status=EnumReadStatus.UNREAD,
            locator="fixture",
            reason="no snapshot file",
        )
        if s.name is EnumSourceName.LINEAR_SNAPSHOT
        else s
        for s in request().sources
    )
    result = _run(sources=sources, linear_issues=())
    bugs = _metric(_day(result, "2026-10-29"), "bugs.opened")
    assert bugs.value is None
    assert bugs.status is EnumReadStatus.UNREAD
    assert "no snapshot file" in bugs.note
    assert bugs.sources[0].name is EnumSourceName.LINEAR_SNAPSHOT


@pytest.mark.unit
def test_day_outside_a_sources_coverage_is_unread_and_a_gap_makes_a_day_partial() -> (
    None
):
    result = _run()
    before_ledger = _metric(_day(result, "2026-10-27"), "friction.rows")
    assert before_ledger.status is EnumReadStatus.UNREAD
    assert before_ledger.value is None
    gap_day = _metric(_day(result, "2026-10-28"), "reds.hours_red")
    assert gap_day.status is EnumReadStatus.PARTIAL
    assert gap_day.sources[0].covered_hours == 18.0
    today = _metric(_day(result, "2026-10-30"), "merges.total")
    assert today.status is EnumReadStatus.READ


@pytest.mark.unit
def test_a_metric_with_two_sources_is_unread_when_either_is() -> None:
    sources = tuple(
        s for s in request().sources if s.name is not EnumSourceName.LANDING_DECISIONS
    )
    metric = _metric(
        _day(_run(sources=sources), "2026-10-28"),
        "merges.controller_to_merged_median_min",
    )
    assert metric.status is EnumReadStatus.UNREAD
    assert "landing_decisions" in metric.note


@pytest.mark.unit
def test_reopened_tickets_are_unread_when_the_snapshot_holds_no_state_history() -> None:
    metric = _metric(_day(_run(), "2026-10-29"), "regressions.tickets_reopened")
    assert metric.status is EnumReadStatus.UNREAD
    assert "state history" in metric.note


@pytest.mark.unit
def test_reset_summary_compares_equal_length_windows_around_the_reset() -> None:
    summary = _run().summary
    assert summary.measured is True
    assert summary.before is not None
    assert summary.after is not None
    assert summary.before.hours == summary.after.hours == summary.window_hours == 34.5
    assert summary.before.end == summary.after.start == summary.reset_at
    assert summary.after.end == GENERATED_AT
    by_key = {c.key: c for c in summary.comparisons}
    assert by_key["andon.hours_stopped"].verdict is EnumVerdict.WORSE
    assert by_key["bugs.opened"].after_status is EnumReadStatus.READ
    assert by_key["lanes.terminals"].verdict is EnumVerdict.INFORMATIONAL
    informational = sum(
        1 for c in summary.comparisons if c.verdict is EnumVerdict.INFORMATIONAL
    )
    assert (
        summary.improved
        + summary.worse
        + summary.flat
        + summary.unmeasured
        + informational
        == len(summary.comparisons)
    )


@pytest.mark.unit
def test_reset_summary_window_shrinks_to_the_time_since_the_reset() -> None:
    result = _run(generated_at=at(29, 11, 30))
    assert result.summary.window_hours == 10.0
    assert result.summary.before is not None
    assert result.summary.before.start == result.summary.reset_at - timedelta(hours=10)


@pytest.mark.unit
def test_reset_summary_is_unmeasured_before_the_reset() -> None:
    result = _run(generated_at=at(28, 12))
    assert result.summary.measured is False
    assert result.summary.comparisons == ()


@pytest.mark.unit
def test_summary_comparison_marks_an_unread_side_unmeasured() -> None:
    sources = tuple(
        s for s in request().sources if s.name is not EnumSourceName.LINEAR_SNAPSHOT
    )
    comparison = next(
        c for c in _run(sources=sources).summary.comparisons if c.key == "bugs.opened"
    )
    assert comparison.verdict is EnumVerdict.UNMEASURED
    assert comparison.before is None
    assert comparison.after is None


@pytest.mark.unit
def test_cost_cell_parsing() -> None:
    assert cost_minutes("~1 lane-minute") == 1.0
    assert (
        cost_minutes("est 140 lane-minutes (free slots x next 20m interval)") == 140.0
    )
    assert cost_minutes("est ~0.1 lane-hours; idle") == pytest.approx(6.0)
    assert cost_minutes("2-4 hours") == 180.0
    assert cost_minutes("unknown") is None


@pytest.mark.unit
def test_determinism_same_input_gives_byte_identical_json() -> None:
    first = _run().model_dump_json(indent=2)
    second = _run().model_dump_json(indent=2)
    assert first == second
    assert json.loads(first)["schema_version"] == 1
