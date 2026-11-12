# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rows window is read from the store once and later pages are cut from the held copy (OMN-24031).

A rows query that carries page_size reads its window from the store once; the
later pages (carrying the cursor) are cut from the held copy instead of
re-reading the store. The cursor now also names the window's end (``u``), so a
later page reads to the same until even when the node's clock has moved on.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_rows_window_cache import (
    ModelRowsWindowCacheSettings,
    RowsWindowCache,
)

pytestmark = pytest.mark.unit

K = EnumWorkLedgerQueryKind
NOW = datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC)
SINCE = datetime(2026, 11, 9, 0, 0, 0, tzinfo=UTC)


def _status_rows() -> tuple[str, ...]:
    """Twelve STATUS rows, one per minute, so page_size=2 gives six pages."""
    return tuple(
        f"2026-11-09T10:{minute:02d}:00Z | STATUS | lane=lane-a | scenario=r{minute} | green"
        for minute in range(12)
    )


class CountingReader(InMemoryWorkLedgerQueryReader):
    """The in-memory reader that counts how many times its window is read."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.window_reads = 0

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[object, ...]:
        self.window_reads += 1
        return super().read_rows(since=since, until=until)  # type: ignore[return-value]

    def read_rows_matching(
        self,
        *,
        since: datetime | None,
        until: datetime,
        kinds: tuple[str, ...],
        contains: str | None,
        term: str | None = None,
    ) -> tuple[object, ...]:
        # the parent's narrowed read goes through read_rows, which counts it
        return super().read_rows_matching(  # type: ignore[return-value]
            since=since, until=until, kinds=kinds, contains=contains, term=term
        )


class FailingReader(CountingReader):
    """A reader whose window read raises while its ``fail`` flag is set."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.fail = False

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[object, ...]:
        if self.fail:
            self.window_reads += 1
            raise RuntimeError("boom")
        return super().read_rows(since=since, until=until)  # type: ignore[return-value]

    def read_rows_matching(
        self,
        *,
        since: datetime | None,
        until: datetime,
        kinds: tuple[str, ...],
        contains: str | None,
        term: str | None = None,
    ) -> tuple[object, ...]:
        if self.fail:
            self.window_reads += 1
            raise RuntimeError("boom")
        return super().read_rows_matching(  # type: ignore[return-value]
            since=since, until=until, kinds=kinds, contains=contains, term=term
        )


class FakeClock:
    """A settable clock: calling it returns ``value``; a test moves ``value`` forward."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _node(
    reader: CountingReader,
    *,
    max_windows: int = 2,
    clock: FakeClock | None = None,
    now: datetime = NOW,
) -> HandlerWorkLedgerQuery:
    cache = RowsWindowCache(
        ModelRowsWindowCacheSettings(ttl_seconds=60, max_windows=max_windows),
        clock=clock if clock is not None else FakeClock(),
    )
    return HandlerWorkLedgerQuery(
        reader=reader,
        rows_window_cache=cache,
        now=lambda: now,
    )


def _ingest(reader: CountingReader, rows: tuple[str, ...]) -> None:
    for number, raw in enumerate(rows):
        assert reader.ingest({"row_id": f"row-{number}", "raw_row": raw})


def _reader(rows: tuple[str, ...]) -> CountingReader:
    reader = CountingReader(now=lambda: NOW)
    _ingest(reader, rows)
    return reader


def _ask(**fields: object) -> ModelWorkLedgerQueryRequest:
    return ModelWorkLedgerQueryRequest.model_validate(
        {
            "correlation_id": uuid4(),
            "query": K.ROWS,
            "since": SINCE,
            "kinds": ("STATUS",),
            **fields,
        }
    )


def _pages(
    node: HandlerWorkLedgerQuery, **fields: object
) -> list[ModelWorkLedgerQueryResult]:
    pages: list[ModelWorkLedgerQueryResult] = []
    cursor: str | None = None
    for _ in range(20):
        page = node.handle(_ask(cursor=cursor, **fields))
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return pages
    raise AssertionError("the cursor never ended")


def _decode_cursor(cursor: str) -> dict[str, object]:
    padded = cursor + "=" * (-len(cursor) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()).decode())


def _encode_cursor(payload: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def test_reads_the_window_once_for_every_page() -> None:
    reader = _reader(_status_rows())
    node = _node(reader)
    pages = _pages(node, page_size=2)
    assert reader.window_reads == 1
    assert len(pages) >= 6


def test_same_rows_order_and_total_as_an_uncached_walk() -> None:
    rows = _status_rows()
    cached_reader = _reader(rows)
    cached_node = _node(cached_reader)
    cached_pages = _pages(cached_node, page_size=2)

    clock = FakeClock()
    uncached_reader = _reader(rows)
    uncached_node = _node(uncached_reader, max_windows=1, clock=clock)
    uncached_pages: list[ModelWorkLedgerQueryResult] = []
    cursor: str | None = None
    for _ in range(20):
        clock.advance(120.0)  # jump past the ttl so every page re-reads the store
        page = uncached_node.handle(_ask(cursor=cursor, page_size=2))
        uncached_pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert [line for p in cached_pages for line in p.lines] == [
        line for p in uncached_pages for line in p.lines
    ]
    assert [p.matched for p in cached_pages] == [p.matched for p in uncached_pages]
    assert uncached_reader.window_reads == len(uncached_pages)


def test_cache_miss_still_answers_correctly() -> None:
    rows = _status_rows()
    reader = _reader(rows)
    clock = FakeClock()
    node = _node(reader, clock=clock)
    first = node.handle(_ask(page_size=2))
    assert first.status == "ok"
    assert first.next_cursor is not None

    clock.advance(120.0)  # advance past ttl_seconds so the held window expires
    second = node.handle(_ask(cursor=first.next_cursor, page_size=2))
    assert second.status == "ok"
    assert second.lines == rows[2:4]
    assert reader.window_reads == 2


def test_error_chain_a_store_failure_on_any_page_is_an_error_answer_and_is_not_held() -> (
    None
):
    rows = _status_rows()
    reader = FailingReader(now=lambda: NOW)
    _ingest(reader, rows)
    clock = FakeClock()
    node = _node(reader, clock=clock)

    reader.fail = True
    failed = node.handle(_ask(page_size=2))
    assert failed.status == "error"
    assert failed.error
    assert failed.lines == ()
    assert reader.window_reads >= 1

    reader.fail = False
    recovered = node.handle(_ask(page_size=2))
    assert recovered.status == "ok"
    assert recovered.lines

    reader.fail = False
    clock.advance(120.0)  # let the held window expire, then fail the next read
    reader.fail = True
    expired = node.handle(_ask(cursor=recovered.next_cursor, page_size=2))
    assert expired.status == "error"
    assert expired.lines == ()


def test_cursor_pins_the_window_end() -> None:
    rows = _status_rows()
    reader = _reader(rows)
    node = _node(reader)
    first = node.handle(_ask(page_size=2))
    assert first.next_cursor is not None

    later_reader = _reader(rows)
    later_node = _node(later_reader, now=NOW + timedelta(hours=1))
    second = later_node.handle(_ask(cursor=first.next_cursor, page_size=2))
    assert second.window_until == NOW
    assert second.matched == first.matched


def test_a_cursor_with_no_window_end_still_pages() -> None:
    rows = _status_rows()
    reader = _reader(rows)
    node = _node(reader)
    first = node.handle(_ask(page_size=2))
    assert first.next_cursor is not None

    payload = _decode_cursor(first.next_cursor)
    payload.pop("u", None)
    stripped = _encode_cursor(payload)

    second = node.handle(_ask(cursor=stripped, page_size=2))
    assert second.status == "ok"
    assert second.lines == rows[2:4]


def test_reader_seam_a_window_read_through_the_reader_reads_the_store_once() -> None:
    """Seam: the reader node pages the query node by next_cursor; the store is read once for the
    whole window, and the reader's lines and total are the direct query's, so a cursor or answer
    shape change on either side fails here."""
    from omnimarket.nodes.node_work_ledger_reader_effect.models import (
        EnumReaderStatus,
    )
    from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
        SEAM_NOW,
        SEAM_SINCE,
        QueryNodePointRead,
        reader,
        rows_request,
    )

    store = _reader(_status_rows())
    node = _node(store, now=SEAM_NOW)
    seam = QueryNodePointRead(node)
    window_hours = int((SEAM_NOW - SEAM_SINCE).total_seconds() // 3600)
    result = reader(seam, clock=SEAM_NOW, page_size=2).handle(
        rows_request(window_hours=window_hours)
    )
    assert result.status is EnumReaderStatus.OK, result.reason
    assert result.lines == _status_rows()
    assert result.matched == len(_status_rows())
    assert len(seam.requests) >= 6
    assert store.window_reads == 1
