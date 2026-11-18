# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rows read answers every row of a window from the query node, page by page (OMN-23958).

The reader's rows read names its window_hours; the handler sends since as the injected clock's
now minus the window, page by page, and merges the pages' lines oldest first with the window
total from the first page. A page that errors, has no watermark, or is older than the bound is
unreadable or stale, never an empty answer. The seam test runs the real query node in process
so a shape change on either side fails here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
    answer_from_json,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderEventKind,
    EnumReaderQuery,
    EnumReaderStatus,
    ModelPointAnswer,
    ModelPointQuery,
    ModelReaderOverlay,
    ModelReaderRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import load_overlay
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    WATERMARK,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_reader_effect/overlay.example.yaml"
)

TOPICS = {
    "reader_answered": "onex.evt.omnimarket.work-ledger-reader-answered.v1",
    "reader_refused": "onex.evt.omnimarket.work-ledger-reader-refused.v1",
}


def overlay(**fields: object) -> ModelReaderOverlay:
    base = load_overlay({"ONEX_LEDGER_READER_OVERLAY": str(EXAMPLE)})
    return base.model_copy(update=fields)


def rows_request(window_hours: int = 24, **fields: object) -> ModelReaderRequest:
    return ModelReaderRequest(
        reader="rows",
        query=EnumReaderQuery.ROWS,
        window_hours=window_hours,
        **fields,
    )


def reader(
    read: ProtocolLedgerPointRead, *, clock: datetime = NOW, **fields: object
) -> HandlerWorkLedgerReader:
    return HandlerWorkLedgerReader(
        overlay(moved_readers=("rows",), **fields),
        read,
        InMemoryEventSink(),
        clock=lambda: clock,
        topics=TOPICS,
    )


class PagedPointRead:
    """Serves a list of pages in order, recording every request it is sent."""

    def __init__(self, pages: list[ModelPointAnswer]) -> None:
        self._pages = list(pages)
        self.requests: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        if not self._pages:
            return ModelPointAnswer(error="no-more-pages")
        page = self._pages.pop(0)
        if self._pages:
            page = page.model_copy(
                update={"next_cursor": f"cursor-{len(self.requests)}"}
            )
        return page


def test_pages_in_order() -> None:
    page1 = ModelPointAnswer(
        lines=("line-1", "line-2"),
        matched=3,
        watermark=WATERMARK,
        next_cursor="cursor-1",
    )
    page2 = ModelPointAnswer(
        lines=("line-3",),
        matched=3,
        watermark=WATERMARK,
        next_cursor="cursor-2",
    )
    page3 = ModelPointAnswer(lines=("line-4",), matched=3, watermark=WATERMARK)
    read = PagedPointRead([page1, page2, page3])
    result = reader(read).handle(rows_request(window_hours=24))
    assert [q.cursor for q in read.requests] == [None, "cursor-1", "cursor-2"]
    assert result.lines == ("line-1", "line-2", "line-3", "line-4")
    assert result.matched == 3
    assert result.watermark == WATERMARK
    assert result.status is EnumReaderStatus.OK
    assert result.file_read is False
    first = read.requests[0]
    assert first.query == "rows"
    assert first.since == NOW - timedelta(hours=24)
    assert first.page_size == overlay().page_size


@pytest.mark.parametrize(
    ("label", "pages", "stale", "status", "lines"),
    [
        (
            "a page answer with error set",
            [ModelPointAnswer(error="node-error:boom")],
            False,
            EnumReaderStatus.UNREADABLE,
            (),
        ),
        (
            "a page answer with no watermark",
            [ModelPointAnswer(lines=("line-1",), matched=1)],
            False,
            EnumReaderStatus.UNREADABLE,
            (),
        ),
        (
            "a page whose watermark is older than the bound",
            [ModelPointAnswer(lines=("line-1",), matched=1, watermark=WATERMARK)],
            True,
            EnumReaderStatus.STALE,
            ("line-1",),
        ),
    ],
)
def test_fault_is_never_empty(
    label: str,
    pages: list[ModelPointAnswer],
    stale: bool,
    status: EnumReaderStatus,
    lines: tuple[str, ...],
) -> None:
    bound = overlay().max_watermark_age_seconds
    clock = WATERMARK + timedelta(seconds=bound + 1) if stale else NOW
    read = PagedPointRead(pages)
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(
        overlay(moved_readers=("rows",)),
        read,
        sink,
        clock=lambda: clock,
        topics=TOPICS,
    ).handle(rows_request())
    assert result.status is status, label
    assert result.lines == lines, label
    if status is EnumReaderStatus.UNREADABLE:
        assert result.source == "none", label
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.REFUSED, label
    assert event.topic == TOPICS["reader_refused"], label
    assert EnumReaderEventKind.ANSWERED not in {e.kind for e in sink.events}, label


def test_not_moved() -> None:
    read = PagedPointRead([])
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(
        overlay(moved_readers=()),
        read,
        sink,
        clock=lambda: NOW,
        topics=TOPICS,
    ).handle(rows_request())
    assert result.status is EnumReaderStatus.NOT_MOVED
    assert read.requests == []
    assert sink.events == []


def test_window_hours_rules() -> None:
    with pytest.raises(ValidationError, match="window_hours"):
        ModelReaderRequest(reader="rows", query=EnumReaderQuery.ROWS)
    with pytest.raises(ValidationError, match="window_hours"):
        ModelReaderRequest(
            reader="inbox", query=EnumReaderQuery.INBOX, lane="lane-x", window_hours=24
        )


# The seam test reads test_rows_page_size's own rows (stamped 2026-11-09) plus one stamped
# outside the window (older than SEAM_SINCE); the clock stands at that test's NOW --
# state_fixtures' NOW is a week before the rows and would read them all outside its window.
SEAM_NOW = datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC)
SEAM_SINCE = datetime(2026, 11, 9, 0, 0, 0, tzinfo=UTC)
K = EnumWorkLedgerQueryKind
STATUS_ROWS = (
    "2026-11-09T10:00:00Z | STATUS | lane=lane-a | scenario=r2 | green",
    "2026-11-09T10:05:00Z | STATUS | lane=lane-b | scenario=r4 | green",
    "2026-11-09T10:10:00Z | STATUS | lane=lane-a | scenario=r2 | red",
    "2026-11-09T10:15:00Z | STATUS | lane=lane-c | scenario=r6 | green",
    "2026-11-09T10:20:00Z | STATUS | lane=lane-b | scenario=r4 | red",
)
MSG_ROW = "2026-11-09T10:25:00Z | MSG | from=lane-a | to=lane-b | id=m1 | hello"
# A row stamped older than SEAM_SINCE: it must be absent from every rows answer.
OUTSIDE_ROW = "2026-11-08T10:00:00Z | STATUS | lane=lane-a | scenario=r0 | green"


class QueryNodePointRead:
    """The real query node handler in process: the typed request in, its JSON answer through the
    adapter the real backend uses out, so a shape change on either side fails the seam test."""

    def __init__(self, node: HandlerWorkLedgerQuery) -> None:
        self._node = node
        self.requests: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        correlation = uuid4()
        payload = {
            "correlation_id": str(correlation),
            **query.model_dump(exclude_none=True),
        }
        for empty in ("lanes", "kinds"):
            if not payload.get(empty):
                payload.pop(empty, None)
        result = self._node.handle(ModelWorkLedgerQueryRequest.model_validate(payload))
        return answer_from_json(json.loads(result.model_dump_json()), str(correlation))


def test_query_node_seam() -> None:
    def _node(rows: tuple[str, ...]) -> HandlerWorkLedgerQuery:
        reader = InMemoryWorkLedgerQueryReader(now=lambda: SEAM_NOW)
        for number, raw in enumerate(rows):
            assert reader.ingest({"row_id": f"row-{number}", "raw_row": raw})
        return HandlerWorkLedgerQuery(reader=reader, now=lambda: SEAM_NOW)

    node = _node((*STATUS_ROWS, MSG_ROW, OUTSIDE_ROW))
    # No kinds filter on the direct query, the same as the reader's read port: both answer the
    # window's rows, STATUS and MSG, in the same order.
    direct = node.handle(
        ModelWorkLedgerQueryRequest.model_validate(
            {
                "correlation_id": uuid4(),
                "query": K.ROWS,
                "since": SEAM_SINCE,
                "page_size": 100,
            }
        )
    )
    assert len(direct.lines) == 6

    read = QueryNodePointRead(node)
    # window_hours = SEAM_NOW - SEAM_SINCE, so the reader's since is the same window start the
    # direct query names, and it stays inside the query node's bounded window (until - 3 days).
    result = reader(read, clock=SEAM_NOW, page_size=2).handle(
        rows_request(window_hours=27)
    )
    assert result.status is EnumReaderStatus.OK, result.reason
    assert result.lines == tuple(direct.lines)
    assert result.matched == direct.matched == 6
    assert not any("2026-11-08" in line for line in result.lines)
    assert [q.cursor is None for q in read.requests] == [True, False, False]
    assert {q.since for q in read.requests} == {SEAM_SINCE}
    assert result.file_read is False


def test_golden_and_error_chain_events() -> None:
    golden = PagedPointRead(
        [ModelPointAnswer(lines=("line-1",), matched=5, watermark=WATERMARK)]
    )
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(
        overlay(moved_readers=("rows",)),
        golden,
        sink,
        clock=lambda: NOW,
        topics=TOPICS,
    ).handle(rows_request())
    assert result.status is EnumReaderStatus.OK
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.ANSWERED
    assert event.topic == TOPICS["reader_answered"]
    assert event.matched == 5

    broken = PagedPointRead([ModelPointAnswer(error="node-error:boom")])
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(
        overlay(moved_readers=("rows",)),
        broken,
        sink,
        clock=lambda: NOW,
        topics=TOPICS,
    ).handle(rows_request())
    assert result.status is EnumReaderStatus.UNREADABLE
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.REFUSED
    assert event.topic == TOPICS["reader_refused"]
