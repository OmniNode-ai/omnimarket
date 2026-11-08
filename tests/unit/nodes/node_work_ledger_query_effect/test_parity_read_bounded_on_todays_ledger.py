# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The piece 6 parity read stays inside its bound on today's ledger size (OMN-19609).

The hourly reading writer reads the newest ``work-ledger-parity`` STATUS row through the bus read
command. The request it sends is taken here from the real ``CommandLedgerRowSource``, never retyped.
Before the narrowing the query node read every row of the 36 hour window from the store (about
18,000 rows at the 12,218 rows a day of the 2026-11-07 parity receipt), parsed all of them, and kept
a handful; the node answers one read at a time, so a burst of lane reads queued the parity read past
the bus client's 60 second wait (the 01:15Z tick of 2026-11-08 read UNKNOWN with
``read-command-exit-1:TimeoutError``). The bound is the rows the store hands back, because the
store's cost per read is its rows; the timeout is not touched.

The named injected break is a reader without the narrowing method (the node falls back to the whole
window): the first test then reads the whole window and fails its bound.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest

from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerRowSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerQuery
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    ProtocolWorkLedgerQueryReader,
)

pytestmark = pytest.mark.unit

ROWS_PER_DAY = 12_218  # file_rows of the 2026-11-07 work-ledger parity receipt
WINDOW_END = datetime(2026, 11, 8, 1, 0, tzinfo=UTC)
PARITY_MAX_AGE_SECONDS = (
    129_600  # piece 6 parity source max_age, the 36 hour read window
)
NOW = WINDOW_END + timedelta(minutes=15)
LANE = "work-ledger-parity"
#: The parity read needs the parity rows of the window (one a day, plus the rows that merely
#: name the lane). Far above that and the store is handing back the window whole.
STORE_ROWS_BOUND = 200


class CountingStore:
    """Rows of a ledger sized as today's; counts the rows the store hands the node.

    This store has no ``read_rows_matching``: it is the injected break, a reader without the
    narrowing method, so the node reads the window whole.
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
        return self._hand_back(
            tuple(
                r
                for r in self.records
                if r.row_type == "STATUS"
                and r.row_lane == LANE
                and since <= r.row_ts <= until
            )
        )

    def freshness(self, *, until: datetime) -> tuple[datetime | None, datetime | None]:
        shown = [r for r in self.records if r.row_ts <= until]
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


def _record(
    stamp: datetime, kind: str, lane: str, extra: str
) -> ModelWorkLedgerRowRecord:
    text = (
        f"{stamp.strftime('%Y-%m-%dT%H:%M:%SZ')} | {kind} | lane={lane} | ticket=OMN-1"
        f" | {extra}"
    )
    return ModelWorkLedgerRowRecord(
        row_id=f"{stamp.timestamp():.0f}-{kind}-{lane}-{len(extra)}",
        row_ts=stamp,
        row_type=kind,
        row_lane=lane,
        text=text,
        raw_row=text,
        source="onex-ledger",
        projected_at=stamp + timedelta(seconds=1),
    )


def todays_ledger() -> tuple[ModelWorkLedgerRowRecord, ...]:
    """The 36 hour window at today's rate: mostly STATUS rows of many lanes, a parity row a day."""
    start = WINDOW_END - timedelta(seconds=PARITY_MAX_AGE_SECONDS)
    count = ROWS_PER_DAY * PARITY_MAX_AGE_SECONDS // 86_400
    step = PARITY_MAX_AGE_SECONDS / count
    kinds = ("STATUS", "STATUS", "STATUS", "CLAIM", "MSG", "TERMINAL")
    records: list[ModelWorkLedgerRowRecord] = []
    for index in range(count):
        stamp = start + timedelta(seconds=index * step)
        records.append(
            _record(
                stamp, kinds[index % len(kinds)], f"lane-{index % 97}", f"n={index}"
            )
        )
    for day_offset in (1, 0):
        stamp = WINDOW_END - timedelta(hours=1, days=day_offset)
        records.append(_record(stamp, "STATUS", LANE, "missing=0 | unexplained=0"))
    records.append(  # a row that only names the lane keeps the narrowing honest
        _record(WINDOW_END - timedelta(hours=3), "MSG", "orchestrator", f"to={LANE}")
    )
    return tuple(sorted(records, key=lambda r: r.row_ts))


def piece6_request_as_sent(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The query JSON the real command source sends for the piece 6 parity source."""
    sent: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        sent.update(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(args, 0, stdout='{"rows": []}', stderr="")

    source = CommandLedgerRowSource(["read"], timeout_seconds=120, row_limit=20_000)
    monkeypatch.setattr(subprocess, "run", fake_run)
    source.read(
        ModelLedgerQuery(
            since=WINDOW_END - timedelta(seconds=PARITY_MAX_AGE_SECONDS),
            until=WINDOW_END,
            row_type="STATUS",
            lane=LANE,
        )
    )
    return sent


def parity_read(store: CountingStore, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent = piece6_request_as_sent(monkeypatch)
    request = ModelWorkLedgerQueryRequest.model_validate(
        {"correlation_id": uuid4(), "now": NOW, **sent}
    )
    reader = cast(ProtocolWorkLedgerQueryReader, store)
    result = HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW).handle(request)
    assert result.error is None
    return [row.raw_row for row in result.rows]


def test_ledger_is_sized_as_today(monkeypatch: pytest.MonkeyPatch) -> None:
    records = todays_ledger()
    assert 18_000 <= len(records) <= 19_000
    assert piece6_request_as_sent(monkeypatch)["lane"] == LANE


def test_parity_read_hands_back_few_rows_on_todays_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = NarrowingCountingStore(todays_ledger())
    rows = parity_read(store, monkeypatch)
    assert len(rows) == 2
    assert all(f"lane={LANE}" in row for row in rows)
    assert store.rows_handed_back <= STORE_ROWS_BOUND


def test_parity_read_without_narrowing_reads_the_whole_window_and_misses_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injected break: the same answer, but the store hands back the whole window."""
    store = CountingStore(todays_ledger())
    rows = parity_read(store, monkeypatch)
    assert len(rows) == 2
    assert store.rows_handed_back > 10 * STORE_ROWS_BOUND
