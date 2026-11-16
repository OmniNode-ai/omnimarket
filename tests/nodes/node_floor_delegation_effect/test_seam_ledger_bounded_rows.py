# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the board's rows reads and the bounded rows query of the ledger read node (OMN-23541).

A rows query with no ``since`` used to read the whole ledger table; the node now reads only its declared default window and says so
(``bounded``, ``bounded_window_days`` on the answer). Two pieces share that request and answer: the board's readers send
``ModelPointQuery`` through ``point_read_adapters._request`` and read the answer back with ``answer_from_json``, and the node
validates the request as ``ModelWorkLedgerQueryRequest`` and answers ``ModelWorkLedgerQueryResult``. Each test fails when either
side changes shape: an answer key the adapter reads that the node's model no longer carries, a bounded answer the adapter can no
longer read, or a request with a start that the node starts to bound.

The same requests are read on the real broker by the command named in the PR (``ONEX_LEDGER_BUS_READ_COMMAND``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    _request,
    answer_from_json,
)

from .test_seam_ledger_read_node import (
    READ_AT,
    NodeLedger,
    ledger_with,
    scheduled_row,
)

pytestmark = pytest.mark.unit

#: The keys ``answer_from_json`` reads from the node's answer (point_read_adapters).
ANSWER_KEYS_THE_BOARD_READS = (
    "correlation_id",
    "error",
    "status",
    "lines",
    "freshness",
    "parity",
    "matched",
    "next_cursor",
    "watermark",
)


def test_seam_every_answer_key_the_board_reads_is_a_field_of_the_nodes_answer() -> None:
    fields = set(ModelWorkLedgerQueryResult.model_fields)
    assert set(ANSWER_KEYS_THE_BOARD_READS) <= fields
    assert {"bounded", "bounded_window_days"} <= fields


def test_golden_seam_a_rows_query_with_a_start_is_not_bounded_and_reads_back() -> None:
    ledger = ledger_with(*(scheduled_row(minute) for minute in (1, 2, 3)))
    query = ModelPointQuery(
        query="rows", since=READ_AT - timedelta(days=8), page_size=200
    )
    correlation_id = str(uuid4())
    wire = _request(query, correlation_id)
    answer = ledger.node.handle(ModelWorkLedgerQueryRequest.model_validate(wire))
    assert answer.bounded is False
    assert answer.bounded_window_days is None
    read = answer_from_json(answer.model_dump(mode="json"), correlation_id)
    assert read.error is None
    assert read.matched == 3
    assert len(read.lines) == 3


def test_golden_seam_a_rows_query_with_no_start_is_bounded_and_still_reads_back() -> (
    None
):
    ledger = ledger_with(*(scheduled_row(minute) for minute in (1, 2, 3)))
    correlation_id = str(uuid4())
    wire = _request(ModelPointQuery(query="rows", page_size=200), correlation_id)
    assert "since" not in wire
    answer = ledger.node.handle(ModelWorkLedgerQueryRequest.model_validate(wire))
    assert answer.bounded is True
    assert answer.bounded_window_days is not None
    assert answer.window_since == READ_AT - timedelta(days=answer.bounded_window_days)
    read = answer_from_json(answer.model_dump(mode="json"), correlation_id)
    assert read.error is None
    assert read.matched == 3


def test_error_seam_a_bounded_read_that_fails_is_unread_never_an_empty_pass() -> None:
    class FailingReader(InMemoryWorkLedgerQueryReader):
        def read_rows(
            self, *, since: datetime | None, until: datetime
        ) -> tuple[ModelWorkLedgerRowRecord, ...]:
            raise RuntimeError("db down")

    ledger = NodeLedger(FailingReader(now=lambda: READ_AT))
    read = ledger.read(ModelPointQuery(query="rows", page_size=200))
    assert read.error is not None
    assert read.error.startswith("node-error:")
    assert read.lines == ()
