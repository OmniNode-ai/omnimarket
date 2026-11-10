# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rows read that names a kind, lane or term and has no start is narrowed in the store (OMN-23615).

The query node read the whole table when a rows request named no start: 329,000 rows, about 12 to 14 s on the lab
(measured 2026-11-10: a ``kinds=[TERMINAL]`` read with no start 18.7 s, the same read with a start an hour old 5.2 s of
which 5.1 s is the client's connection), and filtered in Python afterwards. The store now keeps only the asked row
types, lane and term, and names the ledger's oldest row itself so the parity window does not move; the handler's own
filters still run, so the answer does not change. The budget below is what the store may hand back: the matching rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)
from tests.test_golden_chain_work_ledger_query import (
    NOW,
    FakeReader,
    _lane,
    fixture_records,
)

pytestmark = pytest.mark.unit

FILLER_ROWS = 3000
FILLER_KIND = "NOTE"


def _filler() -> tuple[ModelWorkLedgerRowRecord, ...]:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    rows = []
    for index in range(FILLER_ROWS):
        stamp = start + timedelta(seconds=index)
        raw = f"{stamp:%Y-%m-%dT%H:%M:%SZ} | {FILLER_KIND} | lane=filler-{index % 7} | filler row {index}"
        rows.append(
            ModelWorkLedgerRowRecord(
                row_id=work_ledger_row_id(raw),
                row_ts=stamp,
                row_type=FILLER_KIND,
                row_lane=f"filler-{index % 7}",
                text=raw,
                raw_row=raw,
                source="onex-ledger",
                projected_at=stamp,
            )
        )
    return tuple(rows)


class StoreReader(FakeReader):
    """The fake reader with the store's narrowing and its oldest-row read, as the SQL statements answer them."""

    def read_rows_matching(
        self,
        *,
        since: datetime | None,
        until: datetime,
        kinds: tuple[str, ...],
        contains: str | None,
        term: str | None = None,
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        self._check()
        self.calls.append(("rows-matching", kinds, contains, term))
        return tuple(
            r
            for r in self.records
            if (since is None or since <= r.row_ts)
            and r.row_ts <= until
            and (not kinds or r.row_type in kinds)
            and (not contains or contains in r.raw_row)
            and (not term or term.casefold() in r.raw_row.casefold())
        )

    def oldest_row_ts(self, *, until: datetime) -> datetime | None:
        self.calls.append(("oldest", until))
        return min((r.row_ts for r in self.records if r.row_ts <= until), default=None)


class RefusingStoreReader(StoreReader):
    """The injected break: the narrowed statement fails while every other read answers."""

    def read_rows_matching(self, **_: object) -> tuple[ModelWorkLedgerRowRecord, ...]:
        raise WorkLedgerReadError("work ledger read failed")


class NoOldestReader(FakeReader):
    """A store that narrows but cannot name its oldest row."""

    def read_rows_matching(
        self, **kwargs: object
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        raise AssertionError("a store that cannot name its oldest row is read whole")


def _request(**fields: object) -> ModelWorkLedgerQueryRequest:
    return ModelWorkLedgerQueryRequest.model_validate(
        {
            "correlation_id": uuid4(),
            "now": NOW,
            "query": EnumWorkLedgerQueryKind.ROWS,
            **fields,
        }
    )


def _answer(
    reader: FakeReader, request: ModelWorkLedgerQueryRequest
) -> ModelWorkLedgerQueryResult:
    return HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW).handle(request)


def _data() -> tuple[ModelWorkLedgerRowRecord, ...]:
    return (*_filler(), *fixture_records())


def test_budget_a_kind_read_with_no_start_hands_back_only_the_matching_rows() -> None:
    records = _data()
    reader = StoreReader(records)
    result = _answer(reader, _request(kinds=("TERMINAL",), limit=1))
    terminals = sum(1 for r in records if r.row_type == "TERMINAL")
    assert result.error is None
    assert [c[0] for c in reader.calls if c[0].startswith("rows")] == ["rows-matching"]
    assert result.freshness is not None
    assert 0 < result.freshness.rows_read <= terminals < len(records) // 10


def _cases() -> list[dict[str, object]]:
    records = fixture_records()
    lanes = sorted({lane for r in records if (lane := _lane(r.raw_row))})
    kinds = sorted({r.row_type for r in records})
    terms = ["outcome=", "BLOCKED", "handed-off", "no such text anywhere"]
    cases: list[dict[str, object]] = [{"kinds": (kind,)} for kind in kinds]
    cases += [{"lane": lane} for lane in lanes]
    cases += [{"kinds": (kind,), "term": term} for kind in kinds for term in terms]
    cases += [{"term": term} for term in terms]
    cases.append({"query": EnumWorkLedgerQueryKind.RULINGS})
    return cases


def test_same_answer_every_kind_lane_and_term_with_no_start() -> None:
    records = _data()
    cases = _cases()
    assert len(cases) > 20
    for fields in cases:
        request = _request(**fields)
        narrowed = _answer(StoreReader(records), request)
        whole = _answer(FakeReader(records), request)
        assert narrowed.error is None, fields
        assert narrowed.matched == whole.matched, fields
        assert [r.raw_row for r in narrowed.rows] == [r.raw_row for r in whole.rows], (
            fields
        )
        assert narrowed.parity == whole.parity, fields


def test_same_answer_a_term_with_other_than_ascii_is_read_whole() -> None:
    records = _data()
    reader = StoreReader(records)
    request = _request(term="café")
    assert (
        _answer(reader, request).matched
        == _answer(FakeReader(records), request).matched
    )
    assert [c[0] for c in reader.calls if c[0].startswith("rows")] == ["rows"]


def test_error_a_failed_narrowed_read_is_an_error_answer_never_an_empty_one() -> None:
    result = _answer(RefusingStoreReader(_data()), _request(kinds=("TERMINAL",)))
    assert result.error
    assert not result.rows
    assert result.matched is None or result.matched == 0
    assert "work ledger read failed" in result.error


def test_whole_a_store_that_cannot_name_its_oldest_row_is_read_whole() -> None:
    records = _data()
    reader = NoOldestReader(records)
    request = _request(kinds=("TERMINAL",))
    result = _answer(reader, request)
    assert result.error is None
    assert [c[0] for c in reader.calls if c[0].startswith("rows")] == ["rows"]
    assert result.matched == _answer(FakeReader(records), request).matched
