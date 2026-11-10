# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rows read with a start is narrowed in the store, and answers as the whole-window read (OMN-19609).

The query node read every row of the window from work_ledger_rows and filtered kind and lane in Python, so a
seven-day STATUS read of one lane loaded and parsed the whole week (about 8 s a read, measured on the lab on
2026-11-07, one read at a time). The store now keeps only the asked row types and the rows whose text holds the
lane, a superset of what the lane filter keeps (the lane, from or to cell), and the handler still applies its own
filters, so the answer does not change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from tests.test_golden_chain_work_ledger_query import (
    NOW,
    FakeReader,
    _lane,
    fixture_records,
)

pytestmark = pytest.mark.unit

SINCE = datetime(2026, 9, 1, tzinfo=UTC)


class NarrowingReader(FakeReader):
    """The fake reader with the store-side narrowing, as the SQL statement keeps rows."""

    def read_rows_matching(
        self,
        *,
        since: datetime,
        until: datetime,
        kinds: tuple[str, ...],
        contains: str | None,
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        self._check()
        self.calls.append(("rows-matching", kinds, contains))
        return tuple(
            r
            for r in self.records
            if since <= r.row_ts <= until
            and (not kinds or r.row_type in kinds)
            and (not contains or contains in r.raw_row)
        )


def _request(**fields: object) -> ModelWorkLedgerQueryRequest:
    return ModelWorkLedgerQueryRequest.model_validate(
        {"correlation_id": uuid4(), "now": NOW, "since": SINCE, **fields}
    )


def _rows(reader: FakeReader, request: ModelWorkLedgerQueryRequest) -> list[str]:
    result = HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW).handle(request)
    assert result.error is None
    return [r.raw_row for r in result.rows]


def _cases() -> list[dict[str, object]]:
    records = fixture_records()
    lanes = sorted({lane for r in records if (lane := _lane(r.raw_row))})
    kinds = sorted({r.row_type for r in records})
    cases: list[dict[str, object]] = [
        {"query": EnumWorkLedgerQueryKind.ROWS, "kinds": (kind,)} for kind in kinds
    ]
    cases += [{"query": EnumWorkLedgerQueryKind.ROWS, "lane": lane} for lane in lanes]
    cases += [
        {"query": EnumWorkLedgerQueryKind.ROWS, "kinds": (kind,), "lane": lane}
        for kind in kinds
        for lane in lanes
    ]
    cases.append({"query": EnumWorkLedgerQueryKind.RULINGS})
    return cases


def test_rows_narrowed_every_kind_and_lane_answers_as_the_whole_window_read() -> None:
    records = fixture_records()
    cases = _cases()
    assert len(cases) > 20
    for fields in cases:
        request = _request(**fields)
        narrowed = NarrowingReader(records)
        assert _rows(narrowed, request) == _rows(FakeReader(records), request), fields
        assert [c[0] for c in narrowed.calls if c[0].startswith("rows")] == [
            "rows-matching"
        ], fields


def test_rows_narrowed_a_message_to_the_lane_is_kept() -> None:
    records = fixture_records()
    targets = sorted(
        {
            to
            for r in records
            if r.row_type == "MSG"
            for cell in r.raw_row.split(" | ")
            if cell.startswith("to=")
            for to in cell[3:].split(",")
            if to and to != _lane(r.raw_row)
        }
    )
    assert targets, "the fixture holds a MSG addressed to another lane"
    for lane in targets:
        request = _request(
            query=EnumWorkLedgerQueryKind.ROWS, kinds=("MSG",), lane=lane
        )
        answer = _rows(NarrowingReader(records), request)
        assert answer == _rows(FakeReader(records), request)
        assert answer


def test_rows_narrowed_a_read_with_no_start_reads_the_window_whole() -> None:
    reader = NarrowingReader(fixture_records())
    request = ModelWorkLedgerQueryRequest.model_validate(
        {
            "correlation_id": uuid4(),
            "now": NOW,
            "query": EnumWorkLedgerQueryKind.ROWS,
            "kinds": ("STATUS",),
        }
    )
    HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW).handle(request)
    assert [c[0] for c in reader.calls if c[0].startswith("rows")] == ["rows"]
