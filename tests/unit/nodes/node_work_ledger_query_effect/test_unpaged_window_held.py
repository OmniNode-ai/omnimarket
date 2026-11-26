# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An unpaged window read is held while the store is unchanged (OMN-26152).

The running dev runtime spent most of its GIL time rebuilding every row of an unpaged window on
each query. A second identical unpaged query must not read or parse the window again, an
appended row must show on the next query, and the held answer must equal a fresh one.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers import (
    work_ledger_query_replay as replay,
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


class _Clock:
    """A settable clock the reader stamps ``projected_at`` with."""

    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def tick(self) -> None:
        self.value += timedelta(seconds=1)


class _CountingReader(InMemoryWorkLedgerQueryReader):
    def __init__(self, *, now: Callable[[], datetime]) -> None:
        super().__init__(now=now)
        self.window_reads = 0

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        self.window_reads += 1
        return super().read_rows(since=since, until=until)


def _row(minute: int, lane: str = "lane-a") -> str:
    return f"2026-11-09T10:{minute:02d}:00Z | STATUS | lane={lane} | scenario=r{minute} | green"


def _setup(clock: _Clock) -> tuple[_CountingReader, HandlerWorkLedgerQuery]:
    reader = _CountingReader(now=clock)
    for minute in range(12):
        clock.tick()
        assert reader.ingest({"row_id": f"row-{minute}", "raw_row": _row(minute)})
    return reader, HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW)


def _ask(**fields: object) -> ModelWorkLedgerQueryRequest:
    return ModelWorkLedgerQueryRequest.model_validate(
        {"correlation_id": uuid4(), "query": K.ROWS, **fields}
    )


def _same(a: ModelWorkLedgerQueryResult, b: ModelWorkLedgerQueryResult) -> None:
    drop = {"correlation_id"}
    assert a.model_dump(exclude=drop) == b.model_dump(exclude=drop)


def test_second_identical_unpaged_query_does_not_read_or_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    reader, node = _setup(clock)
    parsed: list[int] = []
    real = replay.parse_rows

    def counting(records):  # type: ignore[no-untyped-def]
        records = tuple(records)
        parsed.append(len(records))
        return real(records)

    monkeypatch.setattr(replay, "parse_rows", counting)
    first = node.handle(_ask())
    reads, parses = reader.window_reads, sum(parsed)
    second = node.handle(_ask())
    assert reader.window_reads == reads
    assert sum(parsed) == parses
    assert first.matched == 12
    _same(first, second)


def test_an_appended_row_is_returned_on_the_next_query() -> None:
    clock = _Clock()
    reader, node = _setup(clock)
    assert node.handle(_ask()).matched == 12
    clock.tick()
    assert reader.ingest({"row_id": "row-new", "raw_row": _row(30)})
    after = node.handle(_ask())
    assert after.matched == 13
    assert any("scenario=r30" in r.text for r in after.rows)


def test_a_held_bounded_answer_equals_a_fresh_one_as_the_clock_moves() -> None:
    clock = _Clock()
    reader, node = _setup(clock)
    node.handle(_ask())
    later = NOW + timedelta(
        days=9, hours=22
    )  # the bounded window start passes the old rows
    held_node = HandlerWorkLedgerQuery(reader=reader, now=lambda: later)
    held_node.handle(_ask())
    fresh = HandlerWorkLedgerQuery(reader=reader, now=lambda: later).handle(_ask())
    _same(held_node.handle(_ask()), fresh)


@pytest.mark.parametrize("kind", [K.HOLDS, K.INBOX, K.OPEN_CLAIMS])
def test_other_whole_window_answers_are_held_and_equal(kind: K) -> None:
    clock = _Clock()
    reader, node = _setup(clock)
    first = node.handle(_ask(query=kind, lane="lane-a", lanes=("lane-a",)))
    reads = reader.window_reads
    second = node.handle(_ask(query=kind, lane="lane-a", lanes=("lane-a",)))
    assert reader.window_reads == reads
    _same(first, second)
