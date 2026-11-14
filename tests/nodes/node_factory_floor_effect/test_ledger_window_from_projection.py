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
from collections.abc import Mapping, Sequence
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
) -> ModelReaderResult:
    """The reader node's answer: ok with ``lines``, or the status named."""
    return ModelReaderResult(
        reader=request.reader,
        query=request.query,
        status=status,
        reason="node-answered-fresh",
        source="node",
        lines=tuple(lines),
        matched=len(lines),
        watermark=NOW if status is EnumReaderStatus.OK else None,
        max_watermark_age_s=900,
        file_read=file_read,
    )


SETTLE = 3600


class FakeProjection:
    """The projection behind the reader node: a factory handing out one fresh read per day, each
    answering the lines of the day its ``since`` names, and recording every request."""

    def __init__(self, lines_by_day: Mapping[str, Sequence[str]]) -> None:
        self._lines_by_day = dict(lines_by_day)
        self.requests: list[ModelReaderRequest] = []
        self.reads = 0

    def __call__(self) -> ProjectionRead:
        self.reads += 1
        lines_by_day = self._lines_by_day
        requests = self.requests

        def read(request: ModelReaderRequest) -> ModelReaderResult:
            requests.append(request)
            day = f"{request.since:%Y-%m-%d}" if request.since else ""
            return _result(request, lines=lines_by_day.get(day, ()))

        return read


def _window(
    factory: ProjectionReadFactory,
    *,
    cache: CachedSourceReader | None = None,
    now: datetime = NOW,
    window_days: int = 3,
) -> tuple[ModelFloorLedgerRow, ...]:
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
    )


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
    [day1, day2, day3, today] = factory.requests
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


def test_the_same_lines_answered_for_every_day_are_not_duplicated() -> None:
    """A node that answers the same lines for every window keeps each row once: only the day a
    row is stamped in keeps it."""
    point = _answer(LINES)
    rows = _window(lambda: _handler(point).handle)
    assert [row.lane for row in rows] == ["l1", "l2", "l3"]
    assert len(point.requests) == 4


def test_a_second_call_reads_only_today_through_the_cache() -> None:
    cache, _backend = memory_reader()
    first_factory = FakeProjection(LINES_BY_DAY)
    first = _window(first_factory, cache=cache)
    assert [f"{r.since:%Y-%m-%d}" for r in first_factory.requests] == [
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
        "2026-11-14",
    ]
    assert cache.tally.stores == 3, "today's rows were kept"
    second_factory = FakeProjection(LINES_BY_DAY)
    second = _window(second_factory, cache=cache, now=NOW + timedelta(minutes=5))
    assert [f"{r.since:%Y-%m-%d}" for r in second_factory.requests] == ["2026-11-14"], (
        "a closed day was read from the projection again"
    )
    assert second == first
    assert cache.tally.hits == 3


def test_failing_control_without_a_cache_every_closed_day_is_read_again() -> None:
    first = _window(FakeProjection(LINES_BY_DAY))
    second_factory = FakeProjection(LINES_BY_DAY)
    second = _window(second_factory)
    assert [f"{r.since:%Y-%m-%d}" for r in second_factory.requests] == [
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
        "2026-11-14",
    ], "a closed day was kept without a cache"
    assert second == first


def test_error_reader_not_moved_is_refused_for_the_day() -> None:
    """The overlay moves the read: a reader it does not name keeps its own read, so the
    projection read refuses the day and the point read is never asked."""
    point = _answer(LINES)
    with pytest.raises(SourceRefusedError, match=r"not-moved.*2026-11-11"):
        _window(lambda: _handler(point, moved_readers=("some-other-reader",)).handle)
    assert point.requests == []


def test_error_unreadable_is_refused_never_empty() -> None:
    """The node did not answer: refused with the day and its reason named, never an empty window."""
    with pytest.raises(SourceRefusedError, match=r"unreadable.*2026-11-11"):
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
