# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor's ledger window is read from the work ledger projection one UTC day at a time (OMN-19998, Wave 0).

Golden: with ``window_days=3`` one fresh rows read per day answers the lines of its own day --
three closed days with exact since/until and one open-ended read for today (until None) -- and
the rows land in day order, today last, a row stamped before the window's exact start dropped.
The node's answering the same lines for every day cannot duplicate rows. Cache: with a real
source read cache a second call reads only today (the closed days are kept; the failing control
reads without a cache and repeats every closed day). Error chain: a not-moved, an unreadable and
a stale answer for a closed day is refused naming the day and the status, and an answer that
reports a file read is refused. The zero-file-read guard patches every file path the old read
used (subprocess, ``open()``, ``Path.read_text``, ``Path.open``) and the golden read still
succeeds; the failing control is the file window read itself, which trips the same guard.
"""

from __future__ import annotations

import builtins
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import ModelFloorLedgerRow
from omnimarket.nodes.node_factory_floor_effect.handlers.ledger_window_from_projection import (
    ProjectionRead,
    ProjectionReadFactory,
    read_ledger_window_from_projection,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_ledger_projection_window import (
    ModelProjectionWindow,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    read_ledger_rows,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
    InMemoryPointRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderParity,
    EnumReaderQuery,
    EnumReaderStatus,
    ModelInMemoryReadRoute,
    ModelPointAnswer,
    ModelReaderEvents,
    ModelReaderOverlay,
    ModelReaderParity,
    ModelReaderRequest,
    ModelReaderResult,
)
from tests.nodes.node_source_read_cache_effect.support import memory_reader

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
READER = "factory-floor"
KINDS = frozenset({"CLAIM", "TERMINAL", "STATUS"})
# The reader node's own topics: named so the handler never reads its contract file
# (the zero-file-read guard below would refuse that read).
TOPICS = {
    "reader_answered": "onex.evt.omnimarket.work-ledger-reader-answered.v1",
    "reader_refused": "onex.evt.omnimarket.work-ledger-reader-refused.v1",
}

CLAIM = "2026-11-14T10:00:00Z | CLAIM | lane=l1 | ticket=OMN-1 | worktree=none | text"
TERMINAL = (
    "2026-11-14T10:05:00Z | TERMINAL | lane=l2 | ticket=OMN-2 | worktree=none | done"
)
STATUS = "2026-11-14T10:10:00Z | STATUS | lane=l3 | ticket=OMN-3 | worktree=none | note"
LINES = (CLAIM, TERMINAL, STATUS)

# The closed days of the golden window (window_days=3 from NOW): one row each inside its day,
# plus a row stamped before the window's exact start (2026-11-11T12:00Z), which is dropped.
OLD = "2026-11-11T06:00:00Z | CLAIM | lane=old | ticket=OMN-9 | worktree=none | stale"
DAY1 = "2026-11-11T14:00:00Z | CLAIM | lane=day1 | ticket=OMN-4 | worktree=none | first"
DAY2 = "2026-11-12T09:00:00Z | TERMINAL | lane=day2 | ticket=OMN-5 | worktree=none | second"
DAY3 = (
    "2026-11-13T23:00:00Z | STATUS | lane=day3 | ticket=OMN-6 | worktree=none | third"
)
LINES_BY_DAY: dict[str, tuple[str, ...]] = {
    "2026-11-11": (OLD, DAY1),
    "2026-11-12": (DAY2,),
    "2026-11-13": (DAY3,),
    "2026-11-14": LINES,
}


def _overlay(**changes: object) -> ModelReaderOverlay:
    overlay = ModelReaderOverlay(
        moved_readers=(READER,),
        max_watermark_age_seconds=900,
        page_size=1000,
        max_pages=10,
        rows_lines_only=True,
        route=ModelInMemoryReadRoute(kind="inmemory"),
        parity=ModelReaderParity(receipt_lane="work-ledger-parity", window_hours=48),
        events=ModelReaderEvents(journal_env="X_JOURNAL"),
    )
    return overlay.model_copy(update=changes) if changes else overlay


def _handler(
    point_read: InMemoryPointRead, **changes: object
) -> HandlerWorkLedgerReader:
    return HandlerWorkLedgerReader(
        _overlay(**changes),
        point_read,
        InMemoryEventSink(),
        clock=lambda: NOW,
        topics=TOPICS,
    )


def _result(
    request: ModelReaderRequest,
    *,
    lines: Sequence[str] = (),
    status: EnumReaderStatus = EnumReaderStatus.OK,
    file_read: bool = False,
    matched: int | None = None,
    parity: EnumReaderParity = EnumReaderParity.EXACT,
) -> ModelReaderResult:
    """The reader node's answer: ok with ``lines``, or the status named; ``matched`` is the rows the node says it matched."""
    return ModelReaderResult(
        reader=request.reader,
        query=request.query,
        status=status,
        reason="node-answered-fresh",
        source="node",
        lines=tuple(lines),
        matched=len(lines) if matched is None else matched,
        watermark=NOW if status is EnumReaderStatus.OK else None,
        max_watermark_age_s=900,
        parity=parity,
        file_read=file_read,
    )


SETTLE = 3600


class FakeClock:
    """A controllable clock the fake reads advance: ``now`` is its time in seconds."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeProjection:
    """The projection behind the reader node: a factory handing out one fresh read per day, each
    answering the lines of the day its ``since`` names, and recording every request.

    ``matched_by_day`` overrides the rows the node says it matched for a day; ``parity`` is what a
    parity read answers; each read advances ``clock`` by ``tick`` seconds."""

    def __init__(
        self,
        lines_by_day: Mapping[str, Sequence[str]],
        *,
        matched_by_day: Mapping[str, int] | None = None,
        parity: EnumReaderParity = EnumReaderParity.EXACT,
        clock: FakeClock | None = None,
        tick: float = 0.0,
    ) -> None:
        self._lines_by_day = dict(lines_by_day)
        self._matched_by_day = dict(matched_by_day or {})
        self._parity = parity
        self._clock = clock
        self._tick = tick
        self.requests: list[ModelReaderRequest] = []
        self.reads = 0

    def __call__(self) -> ProjectionRead:
        self.reads += 1

        def read(request: ModelReaderRequest) -> ModelReaderResult:
            self.requests.append(request)
            day = f"{request.since:%Y-%m-%d}" if request.since else ""
            result = _result(
                request,
                lines=self._lines_by_day.get(day, ()),
                matched=self._matched_by_day.get(day),
                parity=self._parity,
            )
            if self._clock is not None:
                self._clock.now += self._tick
            return result

        return read

    @property
    def days(self) -> list[str]:
        """The day each read asked for, in the order the reads were made."""
        return [f"{r.since:%Y-%m-%d}" for r in self.requests if r.since is not None]


def _window_full(
    factory: ProjectionReadFactory,
    *,
    cache: CachedSourceReader | None = None,
    now: datetime = NOW,
    window_days: int = 3,
    deadline_seconds: int = 600,
    clock: Callable[[], float] | None = None,
) -> ModelProjectionWindow:
    extra = {} if clock is None else {"clock": clock}
    return read_ledger_window_from_projection(
        reader=READER,
        read_factory=factory,
        window_days=window_days,
        now=now,
        kinds=KINDS,
        cache=cache,
        cache_ttl_seconds=900,
        read_workers=None,
        settle_seconds=SETTLE,
        deadline_seconds=deadline_seconds,
        **extra,
    )


def _window(
    factory: ProjectionReadFactory,
    *,
    cache: CachedSourceReader | None = None,
    now: datetime = NOW,
    window_days: int = 3,
) -> tuple[ModelFloorLedgerRow, ...]:
    return _window_full(factory, cache=cache, now=now, window_days=window_days).rows


def _answer(lines: tuple[str, ...], watermark: datetime = NOW) -> InMemoryPointRead:
    return InMemoryPointRead(
        {"rows": ModelPointAnswer(lines=lines, matched=len(lines), watermark=watermark)}
    )


def test_golden_one_rows_read_per_day_and_the_rows_land_in_day_order() -> None:
    factory = FakeProjection(LINES_BY_DAY)
    rows = _window(factory)
    assert all(isinstance(row, ModelFloorLedgerRow) for row in rows)
    assert [(row.kind, row.lane) for row in rows] == [
        ("CLAIM", "day1"),
        ("TERMINAL", "day2"),
        ("STATUS", "day3"),
        ("CLAIM", "l1"),
        ("TERMINAL", "l2"),
        ("STATUS", "l3"),
    ]
    # one fresh read per day: three closed days with exact since/until, today open-ended
    [today, day1, day2, day3] = factory.requests
    assert factory.reads == 4
    for request in factory.requests:
        assert request.reader == READER
        assert request.query is EnumReaderQuery.ROWS
    assert (day1.since, day1.until) == (
        datetime(2026, 11, 11, tzinfo=UTC),
        datetime(2026, 11, 12, tzinfo=UTC),
    )
    assert (day2.since, day2.until) == (
        datetime(2026, 11, 12, tzinfo=UTC),
        datetime(2026, 11, 13, tzinfo=UTC),
    )
    assert (day3.since, day3.until) == (
        datetime(2026, 11, 13, tzinfo=UTC),
        datetime(2026, 11, 14, tzinfo=UTC),
    )
    assert (today.since, today.until) == (datetime(2026, 11, 14, tzinfo=UTC), None)


def test_every_day_read_writes_the_readers_summary_line_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _window(FakeProjection(LINES_BY_DAY))
    err_lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("READER ")
    ]
    # today and three closed days: one summary line each, every one naming that no file was read
    assert len(err_lines) == 4
    assert all(f"reader={READER} " in line for line in err_lines)
    assert all(line.endswith("file_read=no") for line in err_lines)


def test_the_same_lines_answered_for_every_day_are_not_duplicated() -> None:
    """A node that answers the same lines for every window keeps each row once: only the day a
    row is stamped in keeps it."""
    point = _answer(LINES)
    rows = _window(lambda: _handler(point).handle)
    assert [row.lane for row in rows] == ["l1", "l2", "l3"]
    # one rows read per day, and the parity read of today's answer
    assert len(point.requests) == 5


def test_a_second_call_reads_only_today_through_the_cache() -> None:
    cache, _backend = memory_reader()
    first_factory = FakeProjection(LINES_BY_DAY)
    first = _window(first_factory, cache=cache)
    assert first_factory.days == [
        "2026-11-14",
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
    ], "today is read before any closed day"
    assert cache.tally.stores == 3, "today's rows were kept"
    second_factory = FakeProjection(LINES_BY_DAY)
    second = _window(second_factory, cache=cache, now=NOW + timedelta(minutes=5))
    assert second_factory.days == ["2026-11-14"], (
        "a closed day was read from the projection again"
    )
    assert second == first
    assert cache.tally.hits == 3


def test_failing_control_without_a_cache_every_closed_day_is_read_again() -> None:
    first = _window(FakeProjection(LINES_BY_DAY))
    second_factory = FakeProjection(LINES_BY_DAY)
    second = _window(second_factory)
    assert second_factory.days == [
        "2026-11-14",
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
    ], "a closed day was kept without a cache"
    assert second == first


def test_error_reader_not_moved_is_refused_for_the_day() -> None:
    """The overlay moves the read: a reader it does not name keeps its own read, so the
    projection read refuses the day and the point read is never asked."""
    point = _answer(LINES)
    with pytest.raises(SourceRefusedError, match=r"not-moved.*2026-11-14"):
        _window(lambda: _handler(point, moved_readers=("some-other-reader",)).handle)
    assert point.requests == []


def test_error_unreadable_is_refused_never_empty() -> None:
    """The node did not answer: refused with the day and its reason named, never an empty window."""
    with pytest.raises(SourceRefusedError, match=r"unreadable.*2026-11-14"):
        _window(lambda: _handler(InMemoryPointRead()).handle)


def test_error_stale_today_is_refused() -> None:
    """Today's watermark is the projection's own lag: older than the bound, the node answered but the read is refused."""
    point = _answer(LINES, watermark=NOW - timedelta(hours=2))
    with pytest.raises(SourceRefusedError, match=r"stale"):
        _window(lambda: _handler(point).handle, window_days=0)


def test_a_closed_day_answering_stale_is_read_whole() -> None:
    """A closed day's watermark is its newest row, so the node always answers stale for it: its rows are kept (measured on the dev bus)."""

    def factory() -> ProjectionRead:
        def read(request: ModelReaderRequest) -> ModelReaderResult:
            day = f"{request.since:%Y-%m-%d}"
            closed = request.until is not None
            return _result(
                request,
                lines=LINES_BY_DAY.get(day, ()),
                status=EnumReaderStatus.STALE if closed else EnumReaderStatus.OK,
            )

        return read

    rows = _window(factory)
    assert [row.lane for row in rows][:3] == ["day1", "day2", "day3"]


def test_failing_control_a_closed_day_unreadable_is_still_refused() -> None:
    """Only stale is accepted for a closed day: an unreadable closed day is refused, so the stale rule is not a blanket accept."""

    def factory() -> ProjectionRead:
        return lambda request: _result(
            request,
            status=EnumReaderStatus.UNREADABLE
            if request.until is not None
            else EnumReaderStatus.OK,
        )

    with pytest.raises(SourceRefusedError, match=r"unreadable.*2026-11-11"):
        _window(factory)


def test_yesterday_is_not_kept_until_it_has_settled() -> None:
    """Just after midnight yesterday may lack its last rows (the projection lags): it is read live, not cached, until settle_seconds pass."""
    just_after_midnight = NOW.replace(hour=0, minute=10)
    cache, _backend = memory_reader()
    factory = FakeProjection(LINES_BY_DAY)
    _window(factory, cache=cache, now=just_after_midnight)
    first = len(factory.requests)
    _window(factory, cache=cache, now=just_after_midnight)
    repeated = len(factory.requests) - first
    # Only the unsettled yesterday and today are read again; the older closed days come from the cache.
    assert repeated == 2


def test_error_an_answer_reporting_a_file_read_is_refused() -> None:
    """A reader that fell back to the file is refused, whatever its lines."""

    def factory() -> ProjectionRead:
        return lambda request: _result(request, file_read=True)

    with pytest.raises(SourceRefusedError, match="file read"):
        _window(factory)


def _guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("file read")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(Path, "read_text", boom)
    monkeypatch.setattr(Path, "open", boom)


def test_zero_file_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """The projection read opens nothing: the golden read succeeds under the guard (the cache is
    built before the patches are installed)."""
    cache, _backend = memory_reader()
    _guard(monkeypatch)
    factory = FakeProjection(LINES_BY_DAY)
    rows = _window(factory, cache=cache)
    assert [(row.kind, row.lane) for row in rows] == [
        ("CLAIM", "day1"),
        ("TERMINAL", "day2"),
        ("STATUS", "day3"),
        ("CLAIM", "l1"),
        ("TERMINAL", "l2"),
        ("STATUS", "l3"),
    ]
    assert factory.reads == 4


def test_failing_control_the_file_window_read_trips_the_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The guard can fail: the file window read it replaces trips it."""
    _guard(monkeypatch)
    with pytest.raises(AssertionError, match="file read"):
        read_ledger_rows(
            window_days=14,
            cut_chars=1200,
            timeout_seconds=5,
            source="file",
            plugin_root=tmp_path,
            plugin_root_env="CLAUDE_PLUGIN_ROOT",
            ledger_rows_file=None,
        )


# ---------------------------------------------------------------------------------------------------------------
# OMN-24490 review fixes: a closed day is kept only from an answer that is fresh for that day, the answer's
# completeness is compared and named, the read holds to its deadline, and a row at midnight lands in its own day.
# ---------------------------------------------------------------------------------------------------------------

LATE = "2026-11-13T23:59:30Z | STATUS | lane=late | ticket=OMN-7 | worktree=none | the late row"


def test_error_a_lagging_projection_does_not_store_a_short_yesterday() -> None:
    """The projection lags past settle_seconds: today holds no row yet and yesterday lacks its last row. The short day is read
    and counted but never kept; once the projection recovers the next run reads it again and sees the late row."""
    cache, _backend = memory_reader()
    lagging = {**LINES_BY_DAY, "2026-11-14": ()}
    first_factory = FakeProjection(lagging)
    first = _window_full(first_factory, cache=cache)
    assert cache.tally.stores == 0, (
        "a closed day was kept before the projection held a row after it"
    )
    assert not any(row.lane == "late" for row in first.rows)

    recovered = {**LINES_BY_DAY, "2026-11-13": (DAY3, LATE)}
    second_factory = FakeProjection(recovered)
    second = _window_full(second_factory, cache=cache)
    assert "2026-11-13" in second_factory.days, "yesterday came from the cache"
    assert any(row.lane == "late" for row in second.rows)
    assert cache.tally.stores == 3, (
        "the closed days were kept once the projection held them"
    )
    third_factory = FakeProjection(recovered)
    _window_full(third_factory, cache=cache)
    assert third_factory.days == ["2026-11-14"]


def test_failing_control_wall_clock_alone_would_have_kept_the_short_day() -> None:
    """The old rule judged only the wall clock: at NOW yesterday is long past settle_seconds, so it would have been kept
    after the lagging run above. The new rule keeps nothing, so the read can fail."""
    midnight = NOW.replace(hour=0, minute=0)
    assert (NOW - midnight).total_seconds() >= SETTLE
    cache, _backend = memory_reader()
    _window_full(FakeProjection({**LINES_BY_DAY, "2026-11-14": ()}), cache=cache)
    assert cache.tally.stores == 0


def test_a_row_at_exactly_midnight_utc_belongs_to_the_day_it_starts() -> None:
    """A row stamped 00:00:00 UTC is in the day that starts there: the closed day's end is exclusive, its start inclusive.
    The midnight row of today also proves the projection holds a row at the end of yesterday, so yesterday is kept."""
    first_midnight = "2026-11-13T00:00:00Z | CLAIM | lane=mid-y | ticket=OMN-8 | worktree=none | at midnight"
    second_midnight = "2026-11-14T00:00:00Z | CLAIM | lane=mid-t | ticket=OMN-9 | worktree=none | at midnight"
    point = _answer((first_midnight, second_midnight))
    cache, _backend = memory_reader()
    window = _window_full(lambda: _handler(point).handle, cache=cache)
    assert [row.lane for row in window.rows] == ["mid-y", "mid-t"]
    assert window.gaps == ("read parity unread",)
    assert cache.tally.stores == 3
    before = len(point.requests)
    _window_full(lambda: _handler(point).handle, cache=cache)
    again = point.requests[before:]
    assert {q.since for q in again if q.lane is None} == {
        datetime(2026, 11, 14, tzinfo=UTC)
    }, "a closed day was read again"


def test_error_a_day_short_of_the_rows_the_node_matched_is_named_and_not_kept() -> None:
    """The node matched 2 rows for 2026-11-12 and returned 1 line: the gap is named on the window and the day is read again next run."""
    cache, _backend = memory_reader()
    first_factory = FakeProjection(LINES_BY_DAY, matched_by_day={"2026-11-12": 2})
    first = _window_full(first_factory, cache=cache)
    assert first.gaps == ("2026-11-12: 1 of 2 rows returned",)
    assert "2026-11-12: 1 of 2 rows returned" in first.cause()
    assert cache.tally.stores == 2, "the short day was kept"
    second_factory = FakeProjection(LINES_BY_DAY)
    second = _window_full(second_factory, cache=cache)
    assert second_factory.days == ["2026-11-14", "2026-11-12"]
    assert second.gaps == ()
    assert second.cause() == ""


def test_error_read_parity_other_than_exact_is_named() -> None:
    """Today's read asks for the parity verdict, no closed day's does, and a verdict that is not exact is named on the window."""
    unread = FakeProjection(LINES_BY_DAY, parity=EnumReaderParity.UNREAD)
    window = _window_full(unread)
    assert window.gaps == ("read parity unread",)
    assert window.cause() == "read parity unread"
    asked = {f"{r.since:%Y-%m-%d}": r.read_parity for r in unread.requests}
    assert asked == {
        "2026-11-14": True,
        "2026-11-11": False,
        "2026-11-12": False,
        "2026-11-13": False,
    }
    assert _window_full(FakeProjection(LINES_BY_DAY)).gaps == ()


def test_error_a_spent_deadline_reports_the_days_unread() -> None:
    """Each read takes 6 s of a 10 s deadline: today and the first closed day are read, the two that wait behind them are not."""
    clock = FakeClock()
    factory = FakeProjection(LINES_BY_DAY, clock=clock, tick=6.0)
    window = _window_full(factory, deadline_seconds=10, clock=clock)
    assert window.unread_days == ("2026-11-12", "2026-11-13")
    assert factory.days == ["2026-11-14", "2026-11-11"]
    assert [row.lane for row in window.rows] == ["day1", "l1", "l2", "l3"]
    assert "2026-11-12, 2026-11-13" in window.cause()


def test_failing_control_with_time_left_every_day_is_read() -> None:
    clock = FakeClock()
    factory = FakeProjection(LINES_BY_DAY, clock=clock, tick=6.0)
    window = _window_full(factory, deadline_seconds=600, clock=clock)
    assert window.unread_days == ()
    assert factory.days == ["2026-11-14", "2026-11-11", "2026-11-12", "2026-11-13"]


def test_error_a_refused_today_stores_no_closed_day() -> None:
    """Today is read first and refused before any closed day is read or kept (the closed days used to be kept before today's refusal)."""
    cache, _backend = memory_reader()
    requests: list[ModelReaderRequest] = []

    def factory() -> ProjectionRead:
        def read(request: ModelReaderRequest) -> ModelReaderResult:
            requests.append(request)
            return _result(
                request,
                lines=LINES_BY_DAY.get(f"{request.since:%Y-%m-%d}", ()),
                status=EnumReaderStatus.STALE,
            )

        return read

    with pytest.raises(SourceRefusedError, match=r"stale.*2026-11-14"):
        _window_full(factory, cache=cache)
    assert cache.tally.stores == 0
    assert [r.until for r in requests] == [None], "a closed day was read before today"
