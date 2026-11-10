# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rows query that carries page_size is answered one keyset page at a time (OMN-23421).

The delegation board's lab-node reader sends query=rows with lanes, kinds,
page_size and cursor (``ModelPointQuery``); the node used to refuse the request
whole ("page_size applies to the point queries only"), so the board read every
lab-node scenario as unread.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerHistoryRange,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)

pytestmark = pytest.mark.unit

K = EnumWorkLedgerQueryKind
NOW = datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC)
SINCE = datetime(2026, 11, 9, 0, 0, 0, tzinfo=UTC)
STATUS_ROWS = (
    "2026-11-09T10:00:00Z | STATUS | lane=lane-a | scenario=r2 | green",
    "2026-11-09T10:05:00Z | STATUS | lane=lane-b | scenario=r4 | green",
    "2026-11-09T10:10:00Z | STATUS | lane=lane-a | scenario=r2 | red",
    "2026-11-09T10:15:00Z | STATUS | lane=lane-c | scenario=r6 | green",
    "2026-11-09T10:20:00Z | STATUS | lane=lane-b | scenario=r4 | red",
)
MSG_ROW = "2026-11-09T10:25:00Z | MSG | from=lane-a | to=lane-b | id=m1 | hello"


def _node(rows: tuple[str, ...] = (*STATUS_ROWS, MSG_ROW)) -> HandlerWorkLedgerQuery:
    reader = InMemoryWorkLedgerQueryReader(now=lambda: NOW)
    for number, raw in enumerate(rows):
        assert reader.ingest({"row_id": f"row-{number}", "raw_row": raw})
    return HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW)


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


def test_a_rows_query_with_page_size_answers_one_page_with_lines_and_a_cursor() -> None:
    answer = _node().handle(_ask(page_size=2))
    assert answer.status == "ok"
    assert answer.error is None
    assert answer.matched == 5
    assert answer.page_size == 2
    assert answer.lines == STATUS_ROWS[:2]
    assert [r.row_id for r in answer.rows] == ["row-0", "row-1"]
    assert answer.next_cursor is not None


def test_following_next_cursor_reads_every_row_once_in_order() -> None:
    pages = _pages(_node(), page_size=2)
    assert [len(p.lines) for p in pages] == [2, 2, 1]
    assert [line for p in pages for line in p.lines] == list(STATUS_ROWS)
    assert pages[-1].next_cursor is None
    assert all(p.matched == 5 for p in pages)


def test_lanes_are_a_union_and_exclude_other_lanes() -> None:
    pages = _pages(_node(), page_size=10, lanes=("lane-a", "lane-b"))
    lines = [line for p in pages for line in p.lines]
    assert lines == [STATUS_ROWS[i] for i in (0, 1, 2, 4)]
    assert not any("lane=lane-c" in line for line in lines)


def test_kinds_exclude_other_row_types() -> None:
    lines = [ln for p in _pages(_node(), page_size=10) for ln in p.lines]
    assert MSG_ROW not in lines
    assert len(lines) == 5


def test_a_cursor_that_is_not_a_rows_cursor_is_an_error_answer_not_an_empty_one() -> (
    None
):
    answer = _node().handle(_ask(page_size=2, cursor="not-a-cursor"))
    assert answer.status == "error"
    assert answer.error
    assert answer.page_size == 2
    assert answer.lines == ()


def test_a_point_query_cursor_is_refused_on_a_rows_query() -> None:
    import base64
    import json

    foreign = (
        base64.urlsafe_b64encode(
            json.dumps({"q": "holds", "o": NOW.isoformat(), "k": "x"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    answer = _node().handle(_ask(page_size=2, cursor=foreign))
    assert answer.status == "error"
    assert "holds" in (answer.error or "")


def test_request_rules_for_page_size_and_cursor_on_rows() -> None:
    assert _ask(page_size=1).page_size == 1
    history = ModelWorkLedgerHistoryRange(
        ledger_id="ledger", first_seq=1, last_seq=2, sources=("file",)
    )
    with pytest.raises(ValidationError, match="not page_size"):
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid4(),
            query=K.ROWS,
            page_size=1,
            history=history,
            until=NOW,
        )
    with pytest.raises(ValidationError, match="cursor requires"):
        _ask(cursor="abc")
    with pytest.raises(ValidationError, match="rows only"):
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid4(), query=K.NEWEST_PER_LANE, page_size=1
        )


def test_a_rows_query_without_page_size_is_unchanged() -> None:
    answer = _node().handle(_ask())
    assert answer.status == "ok"
    assert answer.lines == ()
    assert answer.matched == 5
    assert len(answer.rows) == 5
    assert answer.next_cursor is None
