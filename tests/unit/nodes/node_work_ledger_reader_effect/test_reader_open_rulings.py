# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A moved reader asks the query node for open rulings (OMN-19609, cutover plan step L6).

The golden chain runs the reader over the real query node handler on a state page that holds one
open RULING: it answers from the node with the answered event. The error chain fails the node's
database read: unreadable, no lines, no file. The failing control is a reader the overlay does
not name as moved: it answers not-moved and reads nothing, so the golden answer needs the move.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import InMemoryEventSink
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderEventKind,
    EnumReaderQuery,
    EnumReaderStatus,
    ModelReaderRequest,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    WATERMARK,
    FakeStateReader,
    ruling_entity,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_chain_events import (
    QueryNodePointRead,
    overlay,
)

pytestmark = pytest.mark.unit


def rulings(reader: str = "open-rulings") -> ModelReaderRequest:
    return ModelReaderRequest(
        reader=reader, query=EnumReaderQuery.OPEN_RULINGS, ticket="OMN-19609"
    )


def test_golden_chain_moved_open_rulings_answers_from_the_query_node() -> None:
    read = QueryNodePointRead(FakeStateReader((ruling_entity(),)))
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(
        overlay(moved_readers=("open-rulings",)), read, sink, clock=lambda: NOW
    ).handle(rulings())
    assert result.status is EnumReaderStatus.OK
    assert result.source == "node"
    assert result.file_read is False
    assert result.matched == 1
    assert result.watermark == WATERMARK
    assert read.requests[0].query == "open-rulings"
    assert read.requests[0].ticket == "OMN-19609"
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.ANSWERED
    assert event.query is EnumReaderQuery.OPEN_RULINGS


def test_error_chain_open_rulings_node_read_fails_unreadable() -> None:
    read = QueryNodePointRead(
        FakeStateReader(error=WorkLedgerReadError("database unavailable"))
    )
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(
        overlay(moved_readers=("open-rulings",)), read, sink, clock=lambda: NOW
    ).handle(rulings())
    assert result.status is EnumReaderStatus.UNREADABLE
    assert result.lines == ()
    assert result.file_read is False
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.REFUSED


def test_control_reader_not_named_as_moved_reads_nothing() -> None:
    read = QueryNodePointRead(FakeStateReader((ruling_entity(),)))
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(
        overlay(moved_readers=("inbox",)), read, sink, clock=lambda: NOW
    ).handle(rulings())
    assert result.status is EnumReaderStatus.NOT_MOVED
    assert result.lines == ()
    assert read.requests == []
    assert sink.events == []


def test_open_rulings_without_a_ticket_is_refused_before_any_read() -> None:
    read = QueryNodePointRead(FakeStateReader((ruling_entity(),)))
    handler = HandlerWorkLedgerReader(
        overlay(moved_readers=("open-rulings",)),
        read,
        InMemoryEventSink(),
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="names its ticket"):
        handler.handle(
            ModelReaderRequest(
                reader="open-rulings", query=EnumReaderQuery.OPEN_RULINGS
            )
        )
    assert read.requests == []
