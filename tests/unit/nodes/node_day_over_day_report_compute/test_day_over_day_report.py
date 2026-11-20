# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The day-over-day report (OMN-26212): a two-day fixture gives exactly the changed entries; a missing source reads UNREADABLE."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.models.model_day_over_day_report import (
    UNREADABLE,
    ModelBoardReading,
    ModelCostReading,
    ModelDayOverDayRequest,
    ModelDayReadings,
    ModelRunningReading,
)
from omnimarket.nodes.node_day_over_day_report_compute.handlers.handler_day_over_day_report import (
    HandlerDayOverDayReport,
)

T = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
LEDGER = "fixture rows"


def _run(change: str, host: str, state: str = "running") -> ModelRunningReading:
    return ModelRunningReading(
        change=change, host=host, state=state, source=LEDGER, read_at=T
    )


def _board(check: str, verdict: str) -> ModelBoardReading:
    return ModelBoardReading(check_id=check, verdict=verdict, source=LEDGER, read_at=T)


def _cost(engine: str, calls: int) -> ModelCostReading:
    return ModelCostReading(
        engine=engine, calls=calls, tokens=None, dollars=None, source=LEDGER, read_at=T
    )


def _request(yesterday: ModelDayReadings, today: ModelDayReadings):
    return ModelDayOverDayRequest(yesterday=yesterday, today=today, as_of=T)


def _fixture() -> ModelDayOverDayRequest:
    yesterday = ModelDayReadings(
        label="2026-11-19",
        running=(_run("omnimarket#1", "h201"), _run("omnimarket#2", "h201")),
        board=(_board("C11", "green"), _board("C12", "red"), _board("C13", "green")),
        cost=(_cost("codex", 4),),
        merges=3,
        merges_source="node_merged_running_count_compute",
    )
    today = ModelDayReadings(
        label="2026-11-20",
        running=(
            _run("omnimarket#1", "h201"),
            _run("omnimarket#2", "h201", "not_running"),
            _run("omnimarket#9", "h053"),
        ),
        board=(_board("C11", "red"), _board("C12", "green"), _board("C13", "green")),
        cost=(_cost("codex", 7),),
        merges=5,
        merges_source="node_merged_running_count_compute",
    )
    return _request(yesterday, today)


def test_two_day_fixture_gives_exactly_the_changed_entries() -> None:
    report = HandlerDayOverDayReport().handle(_fixture())
    assert [e.change for e in report.newly_running.entries] == ["omnimarket#9"]
    assert report.newly_running.entries[0].host == "h053"
    flips = {f.check_id: (f.before, f.after) for f in report.board_changes.entries}
    assert flips == {"C11": ("green", "red"), "C12": ("red", "green")}
    regressions = {(r.kind, r.subject) for r in report.regressions.entries}
    assert regressions == {
        ("stopped_running", "omnimarket#2"),
        ("board_red", "C11"),
    }
    assert report.throughput.merges_today.value == 5
    assert report.throughput.merges_yesterday.value == 3
    assert report.throughput.running_today.value == 2
    assert report.throughput.running_yesterday.value == 2
    codex = report.cost_trend.entries[0]
    assert codex.engine == "codex"
    assert (codex.calls_yesterday.value, codex.calls_today.value) == (4, 7)
    assert codex.dollars_today.value == UNREADABLE


def test_every_figure_carries_source_and_read_time() -> None:
    report = HandlerDayOverDayReport().handle(_fixture())
    for fig in (report.throughput.merges_today, report.throughput.running_today):
        assert fig.source
        assert fig.read_at == T


def test_ledger_removed_reads_unreadable_never_zero() -> None:
    gone = ModelDayReadings(label="2026-11-20", running=None, board=None, cost=None)
    old = ModelDayReadings(label="2026-11-19", running=None, board=None, cost=None)
    report = HandlerDayOverDayReport().handle(_request(old, gone))
    assert report.newly_running.reading == UNREADABLE
    assert report.board_changes.reading == UNREADABLE
    assert report.regressions.reading == UNREADABLE
    assert report.cost_trend.reading == UNREADABLE
    assert report.throughput.merges_today.value == UNREADABLE
    assert report.throughput.running_today.value == UNREADABLE
    assert report.throughput.running_today.source
    assert report.newly_running.entries == ()


def test_one_missing_day_makes_the_diff_unreadable_only_for_that_part() -> None:
    req = _fixture()
    today = req.today.model_copy(update={"board": None})
    report = HandlerDayOverDayReport().handle(_request(req.yesterday, today))
    assert report.board_changes.reading == UNREADABLE
    assert report.newly_running.reading == "read"
    assert [e.change for e in report.newly_running.entries] == ["omnimarket#9"]


def test_ledger_reader_folds_rows_into_cumulative_day_states() -> None:
    from omnimarket.nodes.node_day_over_day_report_compute.handlers.ledger_readings import (
        read_day,
    )

    text = "\n".join(
        [
            "2026-11-19T10:00:00Z | STATUS | lane=a | pr=omnimarket#1 installed=h201:abc observed=ran",
            "2026-11-19T11:00:00Z | STATUS | lane=a | crit=C11 verdict=green read_at=2026-11-19T11:00:00Z",
            "2026-11-19T12:00:00Z | TERMINAL | lane=a | model=codex | delegated=2 runs=x",
            "2026-11-20T10:00:00Z | STATUS | lane=b | pr=omnimarket#1 installed=none:refused observed=none",
            "2026-11-20T10:01:00Z | STATUS | lane=b | pr=omnimarket#9 installed=h053:def observed=ran",
            "2026-11-20T11:00:00Z | STATUS | lane=b | crit=C11 verdict=red read_at=2026-11-20T11:00:00Z",
            "2026-11-20T12:00:00Z | TERMINAL | lane=b | model=codex | delegated=3 runs=y",
        ]
    )
    request = ModelDayOverDayRequest(
        yesterday=read_day(text, "2026-11-19"),
        today=read_day(text, "2026-11-20"),
        as_of=T,
    )
    report = HandlerDayOverDayReport().handle(request)
    assert [e.change for e in report.newly_running.entries] == ["omnimarket#9"]
    assert [(r.kind, r.subject) for r in report.regressions.entries] == [
        ("stopped_running", "omnimarket#1"),
        ("board_red", "C11"),
    ]
    assert report.cost_trend.entries[0].calls_today.value == 3
    assert report.cost_trend.entries[0].tokens_today.value == UNREADABLE


def test_ledger_reader_reads_a_missing_ledger_or_empty_day_as_unreadable() -> None:
    from omnimarket.nodes.node_day_over_day_report_compute.handlers.ledger_readings import (
        read_day,
    )

    for day in (read_day(None, "2026-11-20"), read_day("", "2026-11-20")):
        assert day.running is None
        assert day.board is None
        assert day.cost is None
