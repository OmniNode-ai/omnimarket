# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rows read may name its window absolutely, so a caller caches one closed day at a time.

``window_hours`` answers a window that ends at the clock's now; a caller that reads one
closed day at a time needs an absolute ``since`` and ``until`` it can cache. The handler
sends the request's since and until to the query node verbatim (until None when the read
names only since), and the request model refuses a window that names neither or both, an
until that is not later than its since, and since or until on any other query. The failing
control is the old window_hours read: it still sends since as the clock's now minus the
window and until None, so the golden needs the absolute window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_work_ledger_reader_effect.adapters import InMemoryEventSink
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderEventKind,
    EnumReaderQuery,
    EnumReaderStatus,
    ModelPointAnswer,
    ModelReaderRequest,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    WATERMARK,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    TOPICS,
    PagedPointRead,
    overlay,
    rows_request,
)

pytestmark = pytest.mark.unit

SINCE = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
UNTIL = datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC)


def since_until_request(**fields: object) -> ModelReaderRequest:
    return ModelReaderRequest(reader="rows", query=EnumReaderQuery.ROWS, **fields)


def one_line_read() -> PagedPointRead:
    return PagedPointRead(
        [ModelPointAnswer(lines=("line-1",), matched=1, watermark=WATERMARK)]
    )


def reader(read: PagedPointRead) -> HandlerWorkLedgerReader:
    return HandlerWorkLedgerReader(
        overlay(moved_readers=("rows",)),
        read,
        InMemoryEventSink(),
        clock=lambda: NOW,
        topics=TOPICS,
    )


def test_golden_since_and_until_are_sent_verbatim() -> None:
    read = one_line_read()
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(
        overlay(moved_readers=("rows",)),
        read,
        sink,
        clock=lambda: NOW,
        topics=TOPICS,
    ).handle(since_until_request(since=SINCE, until=UNTIL))
    assert result.status is EnumReaderStatus.OK
    assert result.source == "node"
    assert result.lines == ("line-1",)
    assert result.matched == 1
    assert read.requests[0].query == "rows"
    assert read.requests[0].since == SINCE
    assert read.requests[0].until == UNTIL
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.ANSWERED
    assert event.topic == TOPICS["reader_answered"]


def test_since_without_until_sends_until_none() -> None:
    read = one_line_read()
    result = reader(read).handle(since_until_request(since=SINCE))
    assert result.status is EnumReaderStatus.OK
    assert read.requests[0].since == SINCE
    assert read.requests[0].until is None


def test_error_window_rules() -> None:
    with pytest.raises(ValidationError, match="window_hours or its since"):
        ModelReaderRequest(reader="rows", query=EnumReaderQuery.ROWS)
    with pytest.raises(ValidationError, match="not both"):
        ModelReaderRequest(
            reader="rows", query=EnumReaderQuery.ROWS, window_hours=24, since=SINCE
        )
    with pytest.raises(ValidationError, match="only for a rows read that names since"):
        ModelReaderRequest(
            reader="rows", query=EnumReaderQuery.ROWS, window_hours=24, until=UNTIL
        )
    with pytest.raises(ValidationError, match="not later than its since"):
        since_until_request(since=SINCE, until=SINCE)
    with pytest.raises(
        ValidationError, match="since and until are only for a rows read"
    ):
        ModelReaderRequest(
            reader="inbox", query=EnumReaderQuery.INBOX, lane="lane-x", since=SINCE
        )


def test_control_window_hours_read_still_sends_now_minus_window() -> None:
    read = one_line_read()
    result = reader(read).handle(rows_request(window_hours=24))
    assert result.status is EnumReaderStatus.OK
    assert read.requests[0].since == NOW - timedelta(hours=24)
    assert read.requests[0].until is None
