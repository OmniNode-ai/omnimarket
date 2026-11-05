# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the reading writer (OMN-22478).

Written by hand in the shape of plan section 3.2 (publish the input event, assert every event it
produces in order under one correlation id, ending in the success event; inject a defined fault
and assert the typed failure event, a ledger failure row and the NOT WORKING reading). The chain
test generator (lane b0-chain-tests) converts them when it lands: each ``_Case`` below is a
scenario with a fixture, a fault and independent expectations, which is the generator's input.

Each test fails for real: the last block injects named breaks (drop the terminal event, a wrong
correlation id, a skipped replay check) and asserts that the chain assertions turn red.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
    answer_command,
    load_topics,
)
from tests.nodes.node_reading_writer_effect.reading_support import (
    END,
    RUN_AT,
    START,
    healthy_ledger,
    overlay,
    parity_row,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "reading_piece_written",
        "reading_piece_failed",
        "reading_window_completed",
        "reading_window_failed",
    )
]
CORRELATION = "chain-1"


def _command(**changes: object) -> bytes:
    body: dict[str, object] = {
        "correlation_id": CORRELATION,
        "window_start": START.isoformat(),
        "pieces": [],
        "write": True,
    }
    body.update(changes)
    return json.dumps(body).encode()


@dataclass
class _Case:
    ledger: InMemoryLedger
    payload: bytes
    at: object = RUN_AT
    writer_type: type[HandlerReadingWriter] = HandlerReadingWriter


async def _publish(case: _Case) -> list[tuple[str, dict[str, object]]]:
    """Publish the command on the in-memory bus; return every event produced, in order."""
    writer = case.writer_type(
        overlay=overlay(),
        source=case.ledger,
        appender=case.ledger,
        clock=lambda: case.at,  # type: ignore[arg-type,return-value]
    )
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, object]]] = []
    done = asyncio.Event()

    async def on_command(message: object) -> None:
        for topic, body in answer_command(message.value, writer, TOPICS):  # type: ignore[attr-defined]
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str):  # type: ignore[no-untyped-def]
        async def collect(message: object) -> None:
            seen.append((topic, json.loads(message.value)))  # type: ignore[attr-defined]
            if topic in (
                TOPICS["reading_window_completed"],
                TOPICS["reading_window_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["reading_window_requested"],
            on_message=on_command,
            group_id="reading",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["reading_window_requested"], key=None, value=case.payload
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def _reading_rows(ledger: InMemoryLedger) -> list[str]:
    return [line for line in ledger.lines if "src=reading" in line]


# -- assertions the chain tests share; the last block shows each one can fail ----------------


def assert_golden_chain(
    events: list[tuple[str, dict[str, object]]], pieces: list[int]
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [TOPICS["reading_piece_written"]] * len(pieces) + [
        TOPICS["reading_window_completed"]
    ], topics
    assert topics[-1] == "onex.evt.omnimarket.reading-window-completed.v1"
    assert all(
        topic == "onex.evt.omnimarket.reading-piece-written.v1" for topic in topics[:-1]
    )
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert [body["piece"] for _, body in events[:-1]] == pieces
    assert events[-1][1]["failed"] == 0


def assert_failed_piece_chain(
    events: list[tuple[str, dict[str, object]]],
    ledger: InMemoryLedger,
    *,
    piece: int,
    outcome: str,
    verdict: str,
) -> None:
    """A typed failure event for the piece, then the run's completion, under one correlation id."""
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    failures = [
        body for topic, body in events if topic == TOPICS["reading_piece_failed"]
    ]
    assert any(
        topic == "onex.evt.omnimarket.reading-piece-failed.v1" for topic, _ in events
    )
    assert [f["piece"] for f in failures] == [piece], events
    assert failures[0]["outcome"] == outcome
    assert events[-1][0] == TOPICS["reading_window_completed"]
    assert events[-1][1]["failed"] == 1
    row = next(
        evidence_row_from_text(line)
        for line in reversed(_reading_rows(ledger))
        if f"piece={piece}" in line
    )
    assert row is not None
    assert row.cells["outcome"] == outcome
    assert row.cells["verdict"] == verdict


def test_golden_chain_one_window_produces_every_reading_in_order() -> None:
    case = _Case(InMemoryLedger(healthy_ledger()), _command(pieces=[6, 15]))
    events = asyncio.run(_publish(case))
    assert_golden_chain(events, [6, 15])
    assert len(_reading_rows(case.ledger)) == 2
    assert [body["verdict"] for _, body in events[:-1]] == ["WORKING", "WORKING"]


def test_golden_chain_all_pieces_including_the_unknown_one() -> None:
    case = _Case(InMemoryLedger(healthy_ledger()), _command())
    events = asyncio.run(_publish(case))
    assert_golden_chain(events, [1, 6, 15])
    assert [body["verdict"] for _, body in events[:-1]] == [
        "UNKNOWN",
        "WORKING",
        "WORKING",
    ]


def test_error_chain_a_bad_payload_ends_in_the_typed_window_failure() -> None:
    case = _Case(InMemoryLedger(healthy_ledger()), b'{"window_start": "not a time"}')
    events = asyncio.run(_publish(case))
    assert [topic for topic, _ in events] == [TOPICS["reading_window_failed"]]
    assert events[0][0] == "onex.evt.omnimarket.reading-window-failed.v1"
    assert str(events[0][1]["failure"]).startswith("bad-payload:")
    assert _reading_rows(case.ledger) == []


def test_error_chain_a_window_still_open_ends_in_the_typed_window_failure() -> None:
    case = _Case(
        InMemoryLedger(healthy_ledger()),
        _command(),
        at=END + timedelta(minutes=3),
    )
    events = asyncio.run(_publish(case))
    assert [topic for topic, _ in events] == [TOPICS["reading_window_failed"]]
    assert events[0][1] == {"correlation_id": CORRELATION, "failure": "window-open"}
    assert _reading_rows(case.ledger) == []


def test_error_chain_a_conflicting_duplicate_fails_the_window_for_that_piece() -> None:
    ledger = InMemoryLedger(
        [
            parity_row(START - timedelta(hours=1), req="dup", text="one"),
            parity_row(START - timedelta(hours=1), req="dup", text="two"),
        ]
    )
    events = asyncio.run(_publish(_Case(ledger, _command(pieces=[6]))))
    assert_failed_piece_chain(
        events, ledger, piece=6, outcome="window-failed", verdict="NOT_WORKING"
    )


def test_error_chain_a_replay_with_different_content_fails() -> None:
    ledger = InMemoryLedger(healthy_ledger())
    asyncio.run(_publish(_Case(ledger, _command(pieces=[6]))))
    ledger.lines.append(
        parity_row(START + timedelta(minutes=30), missing=4, req="late")
    )
    events = asyncio.run(_publish(_Case(ledger, _command(pieces=[6]))))
    assert_failed_piece_chain(
        events, ledger, piece=6, outcome="replay-failed", verdict="NOT_WORKING"
    )


def test_error_chain_an_unreadable_ledger_reads_unknown_and_blocks() -> None:
    ledger = InMemoryLedger(healthy_ledger())
    ledger.read_error = "read-command-exit-1"
    events = asyncio.run(_publish(_Case(ledger, _command(pieces=[6]))))
    assert_failed_piece_chain(
        events, ledger, piece=6, outcome="source-unreadable", verdict="UNKNOWN"
    )


def test_error_chain_an_append_that_does_not_land_is_a_typed_failure_with_no_row() -> (
    None
):
    ledger = InMemoryLedger(healthy_ledger())
    ledger.append_status = "error"
    events = asyncio.run(_publish(_Case(ledger, _command(pieces=[6]))))
    failures = [
        body for topic, body in events if topic == TOPICS["reading_piece_failed"]
    ]
    assert len(failures) == 1
    assert str(failures[0]["ledger"]).startswith("append-failed")
    assert _reading_rows(ledger) == []
    assert events[-1][1]["failed"] == 1


# -- injected breaks: each named break must turn a chain assertion red -------------------------


def test_break_drop_the_terminal_event_turns_the_golden_chain_red() -> None:
    case = _Case(InMemoryLedger(healthy_ledger()), _command(pieces=[6, 15]))
    events = asyncio.run(_publish(case))
    with pytest.raises(AssertionError):
        assert_golden_chain(events[:-1], [6, 15])


def test_break_a_wrong_correlation_id_turns_the_golden_chain_red() -> None:
    case = _Case(InMemoryLedger(healthy_ledger()), _command(pieces=[6, 15]))
    events = asyncio.run(_publish(case))
    bent = [
        (t, {**b, "correlation_id": "other"}) if i == 1 else (t, b)
        for i, (t, b) in enumerate(events)
    ]
    with pytest.raises(AssertionError):
        assert_golden_chain(bent, [6, 15])


def test_break_a_reordered_chain_turns_the_golden_chain_red() -> None:
    case = _Case(InMemoryLedger(healthy_ledger()), _command(pieces=[6, 15]))
    events = asyncio.run(_publish(case))
    with pytest.raises(AssertionError):
        assert_golden_chain([events[1], events[0], events[2]], [6, 15])


class _WriterWithoutReplayCheck(HandlerReadingWriter):
    """The injected break: the replay check is skipped, as if the key were never compared."""

    def _existing(self, piece, window_start, window_end, now):  # type: ignore[no-untyped-def]
        return (), None


def test_break_a_skipped_replay_check_turns_the_replay_error_chain_red() -> None:
    ledger = InMemoryLedger(healthy_ledger())
    asyncio.run(_publish(_Case(ledger, _command(pieces=[6]))))
    ledger.lines.append(
        parity_row(START + timedelta(minutes=30), missing=4, req="late")
    )
    events = asyncio.run(
        _publish(
            _Case(ledger, _command(pieces=[6]), writer_type=_WriterWithoutReplayCheck)
        )
    )
    with pytest.raises(AssertionError):
        assert_failed_piece_chain(
            events, ledger, piece=6, outcome="replay-failed", verdict="NOT_WORKING"
        )
