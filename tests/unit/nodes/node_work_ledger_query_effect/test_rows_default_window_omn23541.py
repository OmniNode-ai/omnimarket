# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rows query with no since reads a bounded window, not the whole table (OMN-23541).

Before the change the node read every row of the window and filtered afterwards
(326 thousand rows, 22.5 s). Now the effective start is ``until - window`` and
the store's narrowing read applies. The window's days come from the overlay or
the contract, never from code. The bound is the rows the store hands back.

The named injected break is a reader that ignores ``since`` (hands back every
row): it documents what the node did before the change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    ProtocolWorkLedgerQueryReader,
)

pytestmark = pytest.mark.unit

CONTRACT_PATH = (
    Path(__file__).parents[4]
    / "src/omnimarket/nodes/node_work_ledger_query_effect/contract.yaml"
)


def _contract_window_days() -> int:
    """The window's days, read from the contract (never a literal in code)."""
    with CONTRACT_PATH.open(encoding="utf-8") as handle:
        contract = yaml.safe_load(handle)
    return contract["work_ledger_query"]["rows_default_window_days"]


UNIL = datetime(2026, 11, 8, 1, 0, tzinfo=UTC)


class CountingStore:
    """Rows of a ledger; counts the rows the store hands the node.

    This store has no ``read_rows_matching``: it is the injected break, a reader
    without the narrowing method, so the node reads the window whole.
    """

    def __init__(self, records: tuple[ModelWorkLedgerRowRecord, ...]) -> None:
        self.records = records
        self.rows_handed_back = 0

    def _hand_back(
        self, rows: tuple[ModelWorkLedgerRowRecord, ...]
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        self.rows_handed_back += len(rows)
        return rows

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return self._hand_back(
            tuple(
                r
                for r in self.records
                if (since is None or r.row_ts >= since) and r.row_ts <= until
            )
        )

    def read_parity_receipts(
        self, *, since: datetime, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return ()

    def freshness(self, *, until: datetime) -> tuple[datetime | None, datetime | None]:
        shown = [r for r in self.records if r.row_ts <= until]
        if not shown:
            return None, None
        return max(r.row_ts for r in shown), max(
            r.projected_at for r in shown if r.projected_at is not None
        )


class NarrowingCountingStore(CountingStore):
    """The same store with the narrowing method the node asks for (OMN-19609)."""

    def read_rows_matching(
        self,
        *,
        since: datetime,
        until: datetime,
        kinds: tuple[str, ...],
        contains: str | None,
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return self._hand_back(
            tuple(
                r
                for r in self.records
                if since <= r.row_ts <= until
                and (not kinds or r.row_type in kinds)
                and (not contains or contains in r.raw_row)
            )
        )


class IgnoringSinceStore(CountingStore):
    """The injected break: a reader that ignores ``since`` and hands back every row."""

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return self._hand_back(tuple(r for r in self.records if r.row_ts <= until))


def _record(stamp: datetime, kind: str, lane: str) -> ModelWorkLedgerRowRecord:
    text = (
        f"{stamp.strftime('%Y-%m-%dT%H:%M:%SZ')} | {kind} | lane={lane} | ticket=OMN-1"
    )
    return ModelWorkLedgerRowRecord(
        row_id=f"{stamp.timestamp():.0f}-{kind}-{lane}",
        row_ts=stamp,
        row_type=kind,
        row_lane=lane,
        text=text,
        raw_row=text,
        source="onex-ledger",
        projected_at=stamp + timedelta(seconds=1),
    )


def _ledger(window_days: int) -> tuple[ModelWorkLedgerRowRecord, ...]:
    """Rows older and newer than the window, around UNIL."""
    window_start = UNIL - timedelta(days=window_days)
    records = [
        _record(UNIL - timedelta(days=window_days + 2), "STATUS", "old"),
        _record(UNIL - timedelta(days=window_days + 1), "STATUS", "old"),
        _record(window_start + timedelta(hours=1), "STATUS", "in"),
        _record(UNIL - timedelta(hours=1), "STATUS", "in"),
    ]
    return tuple(sorted(records, key=lambda r: r.row_ts))


def _request(query: str, **kwargs: Any) -> ModelWorkLedgerQueryRequest:
    return ModelWorkLedgerQueryRequest(
        correlation_id=uuid4(), query=query, now=UNIL, until=UNIL, **kwargs
    )


def _answer(
    store: CountingStore, request: ModelWorkLedgerQueryRequest
) -> ModelWorkLedgerQueryResult:
    reader = cast(ProtocolWorkLedgerQueryReader, store)
    return HandlerWorkLedgerQuery(reader=reader, now=lambda: UNIL).handle(request)


def test_golden_no_since_rows_query_is_bounded_to_the_window() -> None:
    days = _contract_window_days()
    store = NarrowingCountingStore(_ledger(days))
    result = _answer(store, _request("rows"))
    assert result.error is None
    assert result.bounded is True
    assert result.bounded_window_days == days
    assert result.window_since == UNIL - timedelta(days=days)
    in_window = [r for r in store.records if r.row_ts >= result.window_since]
    assert len(result.rows) == len(in_window)
    assert all(r.row_ts >= result.window_since for r in result.rows)
    assert not any(r.row_lane == "old" for r in result.rows)
    assert store.rows_handed_back == len(in_window)


def test_golden_explicit_since_is_not_bounded() -> None:
    days = _contract_window_days()
    store = NarrowingCountingStore(_ledger(days))
    result = _answer(store, _request("rows", since=UNIL - timedelta(days=days + 2)))
    assert result.error is None
    assert result.bounded is False
    assert result.bounded_window_days is None
    assert any(r.row_lane == "old" for r in result.rows)


def test_golden_paged_rows_query_is_bounded() -> None:
    days = _contract_window_days()
    store = NarrowingCountingStore(_ledger(days))
    result = _answer(store, _request("rows", page_size=5))
    assert result.error is None
    assert result.bounded is True
    assert result.bounded_window_days == days
    assert all(r.row_ts >= result.window_since for r in result.rows)


def test_overlay_window_wins_over_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("default_rows_window_days: 1\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_LEDGER_QUERY_OVERLAY", str(overlay))
    store = NarrowingCountingStore(_ledger(3))
    result = _answer(store, _request("rows"))
    assert result.bounded is True
    assert result.bounded_window_days == 1
    assert result.window_since == UNIL - timedelta(days=1)


@pytest.mark.parametrize(
    "bad",
    ["default_rows_window_days: 0\n", "default_rows_window_days: x\n", "other: 1\n"],
)
def test_error_an_invalid_overlay_refuses_and_never_falls_back_to_the_contract(
    bad: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(bad, encoding="utf-8")
    monkeypatch.setenv("ONEX_LEDGER_QUERY_OVERLAY", str(overlay))
    with pytest.raises(ValueError, match="invalid for node_work_ledger_query_effect"):
        HandlerWorkLedgerQuery(
            contract_path=CONTRACT_PATH,
            now=lambda: UNIL,
            reader=cast(ProtocolWorkLedgerQueryReader, NarrowingCountingStore(())),
        )


def test_error_answer_of_a_bounded_request_carries_bounded() -> None:
    class RaisingStore(CountingStore):
        def read_rows(
            self, *, since: datetime | None, until: datetime
        ) -> tuple[ModelWorkLedgerRowRecord, ...]:
            raise RuntimeError("db down")

    days = _contract_window_days()
    store = RaisingStore(_ledger(days))
    result = _answer(store, _request("rows"))
    assert result.status == "error"
    assert result.bounded is True
    assert result.rows == ()


def test_injected_break_reader_ignores_since_and_hands_back_every_row() -> None:
    """The injected break: a reader that ignores ``since`` hands back every row."""
    days = _contract_window_days()
    store = IgnoringSinceStore(_ledger(days))
    result = _answer(store, _request("rows"))
    assert result.error is None
    in_window = [r for r in store.records if r.row_ts >= UNIL - timedelta(days=days)]
    assert store.rows_handed_back > len(in_window)


def test_ac2_with_start_answer_equals_the_unchanged_reading() -> None:
    """A query WITH a start answers exactly as the unchanged code did (AC2)."""
    days = _contract_window_days()
    since = UNIL - timedelta(days=days + 3)
    requests = [
        _request("rows", since=since),
        _request("rows", since=since, lane="old"),
        _request("rows", since=since, kinds=["STATUS"]),
    ]
    for request in requests:
        records = _ledger(days)
        expected = sorted(
            r.row_id
            for r in records
            if since <= r.row_ts <= UNIL
            and (request.lane is None or r.row_lane == request.lane)
            and (not request.kinds or r.row_type in request.kinds)
        )
        answers = (
            _answer(CountingStore(records), request),
            _answer(NarrowingCountingStore(records), request),
        )
        assert [r.row_id for r in answers[0].rows] == [
            r.row_id for r in answers[1].rows
        ]
        for result in answers:
            assert result.error is None
            assert result.bounded is False
            assert result.bounded_window_days is None
            assert result.window_since == request.since
            assert sorted(r.row_id for r in result.rows) == expected


def test_ac2_no_start_answer_is_bounded_and_says_older_rows_exist() -> None:
    """A query with NO start is bounded by design and its answer says so (AC2)."""
    days = _contract_window_days()
    store = NarrowingCountingStore(_ledger(days))
    result = _answer(store, _request("rows"))
    assert result.error is None
    assert result.bounded is True
    assert result.bounded_window_days == days
    assert result.window_since == UNIL - timedelta(days=days)
    answer_ids = {r.row_id for r in result.rows}
    older_ids = {r.row_id for r in store.records if r.row_ts < result.window_since}
    assert older_ids
    assert not answer_ids & older_ids
    in_window_ids = {r.row_id for r in store.records if r.row_ts >= result.window_since}
    assert in_window_ids <= answer_ids
