# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A moved reader asks the query node for open claims inside a named window (OMN-24445).

The query node refuses an unwindowed open-claims answer over its byte bound, so a moved
open-claims read may name window_hours: the handler sends claims_since as the injected
clock's now minus the window. The golden chain runs the reader over the real query node
handler on a state page that holds one open CLAIM and reads the claims_since it sent. The
error chain refuses a window on an inbox read at the request model. The failing control is
the same open-claims read without a window: it sends claims_since None, so the golden needs
the window.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import InMemoryEventSink
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    _stamp,
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
    ModelReaderRequest,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    FakeStateReader,
    claim_entity,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_chain_events import (
    QueryNodePointRead,
    overlay,
)

pytestmark = pytest.mark.unit


class StampedQueryNodePointRead(QueryNodePointRead):
    """The query node's handler in process, recording every typed request it is sent. The node's
    own request carries claims_since as a ledger timestamp string, so the typed window is stamped
    the way the bus read command's request does (point_read_adapters._request) before the node
    reads it."""

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        correlation = "4b8b2c55-5f1d-4f5e-9f53-5a8f0c9b7c11"
        payload: dict[str, object] = {
            "correlation_id": correlation,
            **query.model_dump(exclude_none=True),
        }
        for empty in ("lanes", "kinds"):
            if not payload.get(empty):
                payload.pop(empty, None)
        if query.claims_since is not None:
            payload["claims_since"] = _stamp(query.claims_since)
        result = self._node.handle(ModelWorkLedgerQueryRequest.model_validate(payload))
        return answer_from_json(json.loads(result.model_dump_json()), correlation)


def open_claims(**fields: object) -> ModelReaderRequest:
    return ModelReaderRequest.model_validate(
        {"reader": "cutover-owner", "query": "open-claims", **fields}
    )


def test_golden_chain_moved_open_claims_names_the_window() -> None:
    read = StampedQueryNodePointRead(FakeStateReader((claim_entity(),)))
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(
        overlay(moved_readers=("cutover-owner",)), read, sink, clock=lambda: NOW
    ).handle(open_claims(window_hours=6))
    assert result.status is EnumReaderStatus.OK
    assert result.source == "node"
    assert result.file_read is False
    assert read.requests[0].query == "open-claims"
    assert read.requests[0].claims_since == NOW - timedelta(hours=6)
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.ANSWERED
    assert event.query is EnumReaderQuery.OPEN_CLAIMS


def test_error_chain_a_window_on_an_inbox_read_is_refused() -> None:
    with pytest.raises(ValidationError, match="window_hours"):
        ModelReaderRequest(
            reader="inbox", query=EnumReaderQuery.INBOX, lane="lane-x", window_hours=6
        )


def test_control_open_claims_without_a_window_sends_no_claims_since() -> None:
    read = StampedQueryNodePointRead(FakeStateReader((claim_entity(),)))
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(
        overlay(moved_readers=("cutover-owner",)), read, sink, clock=lambda: NOW
    ).handle(open_claims())
    assert result.status is EnumReaderStatus.OK
    assert read.requests[0].claims_since is None
