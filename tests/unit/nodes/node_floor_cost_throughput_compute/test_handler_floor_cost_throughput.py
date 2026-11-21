# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cost-and-throughput panel: a fixture with all sources gives the expected numbers; a fixture with one source removed reads
UNREADABLE for exactly the figures that source feeds, never 0 and never blank."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.models.floor_cost_throughput import (
    ModelCostThroughputPanel,
    ModelCostThroughputRequest,
    ModelDayOverDayDay,
    ModelDayOverDayReport,
    ModelFigure,
    ModelSeenRunningPoint,
    ModelSeenRunningSeries,
    ModelSourceDocument,
)
from omnimarket.nodes.node_floor_cost_throughput_compute.handlers.handler_floor_cost_throughput import (
    HandlerFloorCostThroughput,
)

NOW = "2026-11-20T15:00:00Z"


def _floor_simple(*, read: bool = True) -> ModelSourceDocument:
    return ModelSourceDocument(
        node="node_floor_simple_effect",
        data_read="floor-simple.json numbers[merges]",
        read_at="2026-11-20T14:58:00Z",
        document={
            "numbers": [
                {
                    "key": "open_prs",
                    "value": "3",
                    "read_at": "2026-11-20T14:57:00Z",
                    "read": True,
                },
                {
                    "key": "merges",
                    "value": "7",
                    "read_at": "2026-11-20T14:56:30Z",
                    "read": read,
                },
            ]
        },
    )


def _point(attempts: int, cost: float | None, bucket: str) -> dict[str, Any]:
    return {"bucket": bucket, "attempts": attempts, "cost_usd": cost}


def _series(model: str, hours: list[dict[str, Any]]) -> dict[str, Any]:
    return {"model": model, "task_kind": "*", "hours": hours, "days": []}


def _delegation(
    *, claude_cost: float | None = 3.0, other_cost: float | None = 0.0
) -> ModelSourceDocument:
    return ModelSourceDocument(
        node="node_floor_delegation_history_compute",
        data_read="delegation-history.json models and series[task_kind=*].hours",
        read_at="2026-11-20T14:55:11Z",
        document={
            "window_days": 14,
            "models": [
                {"id": "claude-sonnet-5-5", "engine_kind": "Claude"},
                {"id": "gpt-6.1-sol", "engine_kind": "Codex"},
                {"id": "glm-5.3", "engine_kind": "GLM"},
                {"id": "Qwen3.8-27B@lab-gpu-a", "engine_kind": "local"},
                {"id": "jev-latest", "engine_kind": "other"},
            ],
            "series": [
                _series(
                    "claude-sonnet-5-5",
                    [
                        _point(10, claude_cost, "2026-11-20T10:00:00Z"),
                        _point(
                            5, claude_cost, "2026-11-19T10:00:00Z"
                        ),  # older than 24 h
                    ],
                ),
                _series("gpt-6.1-sol", [_point(4, 1.5, "2026-11-20T11:00:00Z")]),
                _series("glm-5.3", [_point(6, 0.5, "2026-11-20T12:00:00Z")]),
                _series(
                    "Qwen3.8-27B@lab-gpu-a",
                    [_point(20, 0.0, "2026-11-20T13:00:00Z")],
                ),
                _series(
                    "jev-latest", [_point(100, other_cost, "2026-11-20T13:00:00Z")]
                ),
            ],
        },
    )


def _seen(*, span_hours: int = 24) -> ModelSeenRunningSeries:
    # the fold's clock is 15:00 on the 20th; the first reading is span_hours before it (15:00 on the 19th for 24 h)
    start = (
        "2026-11-19T15:00:00Z"
        if span_hours == 24
        else f"2026-11-20T{15 - span_hours:02d}:00:00Z"
    )
    return ModelSeenRunningSeries(
        node="node_floor_simple_effect",
        data_read="floor-simple.json integration.counts.seen_running (kept readings)",
        points=(
            ModelSeenRunningPoint(read_at=start, merged=1000, seen_running=500),
            ModelSeenRunningPoint(
                read_at="2026-11-20T14:59:00Z", merged=1100, seen_running=548
            ),
        ),
    )


def _day_over_day() -> ModelDayOverDayReport:
    return ModelDayOverDayReport(
        node="node_day_over_day_report_compute",
        generated_at="2026-11-20T14:50:00Z",
        days=(
            ModelDayOverDayDay(
                date="2026-11-19",
                merged=200,
                seen_running=150,
                cost_usd=30.0,
                tokens=1_000_000,
            ),
            ModelDayOverDayDay(
                date="2026-11-20",
                merged=100,
                seen_running=48,
                cost_usd=None,
                tokens=None,
            ),
        ),
    )


def _request(**drop: bool) -> ModelCostThroughputRequest:
    parts: dict[str, Any] = {
        "floor_simple": _floor_simple(),
        "delegation_history": _delegation(),
        "seen_running_series": _seen(),
        "day_over_day": _day_over_day(),
    }
    for name in drop:
        parts[name] = None
    return ModelCostThroughputRequest(generated_at=NOW, **parts)


def _figures(panel: ModelCostThroughputPanel) -> dict[str, ModelFigure]:
    found: dict[str, ModelFigure] = {
        panel.merges_per_hour.key: panel.merges_per_hour,
        panel.seen_running_per_hour.key: panel.seen_running_per_hour,
        panel.cost_per_change_seen_running.key: panel.cost_per_change_seen_running,
    }
    for row in panel.engines:
        for figure in (row.calls, row.tokens, row.dollars):
            found[figure.key] = figure
    for day in panel.trend.days:
        for figure in (day.merged, day.seen_running, day.cost_usd):
            found[figure.key] = figure
    return found


def _unreadable(panel: ModelCostThroughputPanel) -> set[str]:
    return {key for key, figure in _figures(panel).items() if figure.value is None}


def test_all_sources_give_the_expected_numbers() -> None:
    panel = HandlerFloorCostThroughput().handle(_request())
    assert panel.merges_per_hour.value == 7
    assert panel.merges_per_hour.source.node == "node_floor_simple_effect"
    assert panel.merges_per_hour.source.read_at == "2026-11-20T14:56:30Z"
    # 548 - 500 = 48 changes seen running over the 23 h 59 min between the two readings
    assert panel.seen_running_per_hour.value == pytest.approx(
        48 / (1439 / 60), rel=1e-6
    )
    by_engine = {row.engine: row for row in panel.engines}
    assert tuple(by_engine) == ("Claude", "Codex", "GLM", "local")
    # only the buckets in the last 24 h count: Claude's 2026-11-19T10:00 bucket is out
    assert by_engine["Claude"].calls.value == 10
    assert by_engine["Codex"].calls.value == 4
    assert by_engine["GLM"].calls.value == 6
    assert by_engine["local"].calls.value == 20
    assert by_engine["Claude"].dollars.value == pytest.approx(3.0)
    assert by_engine["local"].dollars.value == 0.0
    # 3.0 + 1.5 + 0.5 + 0.0 = 5.0 dollars over 48 changes seen running in the same 24 h
    assert panel.cost_per_change_seen_running.value == pytest.approx(5.0 / 48)
    assert panel.trend.status == "read"
    assert [d.date for d in panel.trend.days][-2:] == ["2026-11-19", "2026-11-20"]
    assert len(panel.trend.days) == 7
    by_date = {d.date: d for d in panel.trend.days}
    assert by_date["2026-11-19"].cost_usd.value == 30.0
    assert by_date["2026-11-20"].cost_usd.value is None


def test_tokens_have_no_recorded_source_and_read_unreadable_beside_a_call_count() -> (
    None
):
    panel = HandlerFloorCostThroughput().handle(_request())
    for row in panel.engines:
        assert row.tokens.display == "UNREADABLE"
        assert row.tokens.reason
        assert row.calls.value is not None


@pytest.mark.parametrize(
    ("dropped", "expected"),
    [
        ("floor_simple", {"merges_per_hour"}),
        (
            "seen_running_series",
            {"seen_running_per_hour", "cost_per_change_seen_running"},
        ),
        (
            "delegation_history",
            {
                f"{engine}_{what}"
                for engine in ("Claude", "Codex", "GLM", "local")
                for what in ("calls", "dollars")
            }
            | {"cost_per_change_seen_running"},
        ),
        (
            "day_over_day",
            {
                f"trend_{n}_{what}"
                for n in range(7)
                for what in ("merged", "seen_running", "cost_usd")
            },
        ),
    ],
)
def test_one_source_removed_reads_unreadable_for_exactly_its_figures(
    dropped: str, expected: set[str]
) -> None:
    baseline = HandlerFloorCostThroughput().handle(_request())
    panel = HandlerFloorCostThroughput().handle(_request(**{dropped: True}))
    # the tokens cells and the trend's unreported cells are unreadable in the baseline already
    added = _unreadable(panel) - _unreadable(baseline)
    assert added == expected - _unreadable(baseline)
    for key in expected:
        figure = _figures(panel)[key]
        assert figure.display == "UNREADABLE"
        assert figure.value is None
        assert figure.reason
    # the figures of the sources that remain still read
    assert panel.unreadable_count >= baseline.unreadable_count


def test_a_number_the_page_did_not_read_is_unreadable_not_zero() -> None:
    request = ModelCostThroughputRequest(
        generated_at=NOW,
        floor_simple=_floor_simple(read=False),
        delegation_history=_delegation(),
        seen_running_series=_seen(),
        day_over_day=_day_over_day(),
    )
    panel = HandlerFloorCostThroughput().handle(request)
    assert panel.merges_per_hour.value is None
    assert panel.merges_per_hour.display == "UNREADABLE"


def test_a_dollar_cell_with_an_unrecorded_price_is_unreadable_and_its_calls_still_show() -> (
    None
):
    request = ModelCostThroughputRequest(
        generated_at=NOW,
        floor_simple=_floor_simple(),
        delegation_history=_delegation(claude_cost=None),
        seen_running_series=_seen(),
        day_over_day=_day_over_day(),
    )
    panel = HandlerFloorCostThroughput().handle(request)
    claude = next(row for row in panel.engines if row.engine == "Claude")
    assert claude.calls.value == 10
    assert claude.dollars.value is None
    assert claude.dollars.display == "UNREADABLE"
    # one engine's dollars are unrecorded, so the cost per change is not estimated
    assert panel.cost_per_change_seen_running.value is None


def test_readings_that_do_not_span_a_day_leave_the_per_change_cost_unreadable() -> None:
    request = ModelCostThroughputRequest(
        generated_at=NOW,
        floor_simple=_floor_simple(),
        delegation_history=_delegation(),
        seen_running_series=_seen(span_hours=2),
        day_over_day=_day_over_day(),
    )
    panel = HandlerFloorCostThroughput().handle(request)
    # 48 changes over the 1 h 59 min between the two readings
    assert panel.seen_running_per_hour.value == pytest.approx(48 / (119 / 60), rel=1e-6)
    assert panel.cost_per_change_seen_running.value is None
    assert "24" in panel.cost_per_change_seen_running.reason


def test_every_figure_names_its_source() -> None:
    panel = HandlerFloorCostThroughput().handle(
        _request(floor_simple=True, day_over_day=True)
    )
    for figure in _figures(panel).values():
        assert figure.source.node
        assert figure.source.data_read


def test_unreadable_count_matches_the_figures() -> None:
    panel = HandlerFloorCostThroughput().handle(_request())
    assert panel.unreadable_count == len(_unreadable(panel))
    assert len(panel.unreadable_reasons) == len(set(panel.unreadable_reasons))


def test_the_fold_is_deterministic() -> None:
    first = HandlerFloorCostThroughput().handle(_request()).model_dump_json()
    second = HandlerFloorCostThroughput().handle(_request()).model_dump_json()
    assert first == second
    json.loads(first)


def test_an_engine_outside_the_four_with_an_unrecorded_cost_leaves_the_per_change_cost_unreadable() -> (
    None
):
    request = ModelCostThroughputRequest(
        generated_at=NOW,
        floor_simple=_floor_simple(),
        delegation_history=_delegation(other_cost=None),
        seen_running_series=_seen(),
        day_over_day=_day_over_day(),
    )
    panel = HandlerFloorCostThroughput().handle(request)
    assert panel.cost_per_change_seen_running.value is None
    assert "jev-latest" in panel.cost_per_change_seen_running.reason
    # the four engines' own cells still read
    assert all(row.dollars.value is not None for row in panel.engines)
