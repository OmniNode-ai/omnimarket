# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A held window plus appended rows is extended, not rebuilt (OMN-26152).

About 30 lanes append to the ledger continuously, so the store watermark moves between nearly
every pair of queries. A moved watermark must read only the rows projected since the held read and
append them to the held parse; the answer must equal a full rebuild.
"""

from __future__ import annotations

from collections.abc import Iterable
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
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def tick(self) -> None:
        self.value += timedelta(seconds=1)


def _row(minute: int, lane: str = "lane-a") -> str:
    return f"2026-11-09T10:{minute:02d}:00Z | STATUS | lane={lane} | scenario=r{minute} | green"


def _ask(**fields: object) -> ModelWorkLedgerQueryRequest:
    return ModelWorkLedgerQueryRequest.model_validate(
        {"correlation_id": uuid4(), "query": K.ROWS, **fields}
    )


def _same(a: ModelWorkLedgerQueryResult, b: ModelWorkLedgerQueryResult) -> None:
    drop = {"correlation_id"}
    assert a.model_dump(exclude=drop) == b.model_dump(exclude=drop)


def _setup(
    clock: _Clock,
) -> tuple[InMemoryWorkLedgerQueryReader, HandlerWorkLedgerQuery]:
    reader = InMemoryWorkLedgerQueryReader(now=clock)
    for minute in range(12):
        clock.tick()
        assert reader.ingest({"row_id": f"row-{minute}", "raw_row": _row(minute)})
    return reader, HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW)


def test_appended_rows_are_parsed_alone_and_answer_equals_full_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    reader, node = _setup(clock)
    assert node.handle(_ask()).matched == 12
    parsed: list[int] = []
    real = replay.parse_rows

    def counting(
        incoming: Iterable[ModelWorkLedgerRowRecord],
    ) -> list[replay.LedgerRow]:
        records: tuple[ModelWorkLedgerRowRecord, ...] = tuple(incoming)
        parsed.append(len(records))
        return real(records)

    monkeypatch.setattr(replay, "parse_rows", counting)
    for minute in (30, 31):
        clock.tick()
        assert reader.ingest({"row_id": f"row-{minute}", "raw_row": _row(minute)})
    after = node.handle(_ask())
    assert after.matched == 14
    # the parity receipts parse (0 rows) and the two appended rows; never the 12 held ones
    assert max(parsed, default=0) <= 2
    fresh = HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW).handle(_ask())
    _same(after, fresh)


def test_late_rows_older_than_the_held_tail_are_merged_not_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows from 30 lanes reach the store out of row_ts order, so a late row is the common case."""
    clock = _Clock()
    reader, node = _setup(clock)
    assert node.handle(_ask()).matched == 12
    parsed: list[int] = []
    real = replay.parse_rows

    def counting(
        incoming: Iterable[ModelWorkLedgerRowRecord],
    ) -> list[replay.LedgerRow]:
        records: tuple[ModelWorkLedgerRowRecord, ...] = tuple(incoming)
        parsed.append(len(records))
        return real(records)

    # three requests, each after the watermark moved: a late row, an in-order row, a late row
    answers = []
    with monkeypatch.context() as patch:
        patch.setattr(replay, "parse_rows", counting)
        for n, minute in enumerate((5, 40, 7)):
            clock.tick()
            assert reader.ingest(
                {"row_id": f"row-late-{n}", "raw_row": _row(minute, lane=f"lane-{n}")}
            )
            answers.append(node.handle(_ask()))
    assert [a.matched for a in answers] == [13, 14, 15]
    # a rebuild parses the whole window; each moved watermark must parse only its own row
    assert max(parsed) <= 1
    _same(
        answers[-1],
        HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW).handle(_ask()),
    )


def test_a_sliding_since_finds_the_held_window_and_parses_only_appended_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lab alarm polls with a since that moves each call; the hold must still be found."""
    clock = _Clock()
    reader, node = _setup(clock)
    first = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
    assert node.handle(_ask(since=first)).matched == 12
    parsed: list[int] = []
    real = replay.parse_rows

    def counting(
        incoming: Iterable[ModelWorkLedgerRowRecord],
    ) -> list[replay.LedgerRow]:
        records: tuple[ModelWorkLedgerRowRecord, ...] = tuple(incoming)
        parsed.append(len(records))
        return real(records)

    with monkeypatch.context() as patch:
        patch.setattr(replay, "parse_rows", counting)
        # same watermark, later since: served from the hold, nothing parsed
        later = datetime(2026, 11, 9, 10, 5, 0, tzinfo=UTC)
        slid = node.handle(_ask(since=later))
        assert max(parsed, default=0) == 0
        # a moved watermark and a later since: only the appended row is parsed
        clock.tick()
        assert reader.ingest({"row_id": "row-new", "raw_row": _row(40)})
        moved = node.handle(_ask(since=later + timedelta(minutes=1)))
        assert max(parsed, default=0) <= 1
    assert slid.matched == 7
    assert moved.matched == 7
    _same(
        moved,
        HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW).handle(
            _ask(since=later + timedelta(minutes=1))
        ),
    )


def test_a_paged_rows_poll_with_a_clock_moving_since_and_until_finds_its_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape the dev runtime's query handler was reading (py-spy locals, 2026-11-28): a ROWS
    page request, a ``since`` and an ``until`` that both move with the clock, no lane, kinds or
    term, a store watermark that moves between polls. Every poll must extend the hold."""
    clock = _Clock()
    reader, _ = _setup(clock)
    now = {"value": NOW}
    node = HandlerWorkLedgerQuery(reader=reader, now=lambda: now["value"])

    def poll(since: datetime) -> ModelWorkLedgerQueryResult:
        return node.handle(_ask(since=since, page_size=50))

    assert poll(datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)).matched == 12
    parsed: list[int] = []
    real = replay.parse_rows

    def counting(
        incoming: Iterable[ModelWorkLedgerRowRecord],
    ) -> list[replay.LedgerRow]:
        records: tuple[ModelWorkLedgerRowRecord, ...] = tuple(incoming)
        parsed.append(len(records))
        return real(records)

    with monkeypatch.context() as patch:
        patch.setattr(replay, "parse_rows", counting)
        for n in range(1, 4):
            now["value"] += timedelta(seconds=30)
            clock.tick()
            assert reader.ingest(
                {"row_id": f"row-poll-{n}", "raw_row": _row(40 + n, lane=f"lane-{n}")}
            )
            answer = poll(datetime(2026, 11, 9, 10, n, 0, tzinfo=UTC))
            assert answer.matched == 12  # 12 - n held rows in window + n appended
    # a full read parses the whole window; each poll may parse only its own appended row
    assert max(parsed, default=0) <= 1


def test_the_red_main_alarm_lookup_with_a_new_term_each_call_finds_its_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second caller on the dev runtime (node_red_main_alarm_effect ``recorded``): ROWS,
    kinds STATUS and MSG, ``term=req=<new uuid>`` on every call, ``since`` the 1970 epoch, a page of
    3 lines. The store does not narrow by a term when a start is given, so the term must not
    make a new hold key: a new key per call read and parsed every STATUS and MSG row since 1970,
    and with two held windows evicted the lab alarm's hold as well."""
    clock = _Clock()
    reader, _ = _setup(clock)
    node = HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)

    def recorded(n: int) -> ModelWorkLedgerQueryResult:
        return node.handle(
            _ask(
                kinds=["STATUS", "MSG"],
                term=f"req={uuid4()}-{n}",
                since=epoch,
                page_size=3,
                lines_only=True,
            )
        )

    assert recorded(0).matched == 0
    parsed: list[int] = []
    real = replay.parse_rows

    def counting(incoming):  # type: ignore[no-untyped-def]
        records: tuple[ModelWorkLedgerRowRecord, ...] = tuple(incoming)
        parsed.append(len(records))
        return real(records)

    with monkeypatch.context() as patch:
        patch.setattr(replay, "parse_rows", counting)
        for n in range(1, 4):
            clock.tick()
            assert reader.ingest(
                {"row_id": f"row-red-{n}", "raw_row": _row(40 + n, lane=f"lane-{n}")}
            )
            recorded(n)
    # a read of the window parses all 12 held rows and the new one; an extension parses its own
    assert max(parsed, default=0) <= 1
