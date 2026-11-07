# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the host readings (plan section 3.2, OMN-22478).

Publish the input event and assert every event it produces, in order, under one correlation id,
ending in the success event; inject a defined fault and assert the typed failure event, the ledger
failure row and the reading. The last block shows the chain assertions turn red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_host_reading_effect.handlers.handler_host_reading import (
    HandlerHostReading,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)
from tests.nodes.node_host_reading_effect.host_reading_support import (
    LEDGER_HOST,
    RUN_AT,
    World,
    overlay,
)
from tests.nodes.node_host_reading_effect.test_ledger_durability_probe import (
    AcceptAndDrop,
    Clock,
    DownReader,
    LateStore,
    durability_overlay,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "host_reading_written",
        "host_reading_failed",
        "host_reading_run_completed",
        "host_reading_run_failed",
    )
]
CORRELATION = "chain-1"
PAYLOAD = json.dumps({"correlation_id": CORRELATION, "probes": [], "write": True})


async def _publish(
    world: World,
    ledger: InMemoryLedger,
    payload: bytes,
    host: str = LEDGER_HOST,
    handler: HandlerHostReading | None = None,
) -> list[tuple[str, dict[str, object]]]:
    handler = handler or HandlerHostReading(
        overlay=overlay(),
        host=host,
        files=world.files,
        appender=ledger,
        clock=lambda: RUN_AT,
        environ=world.environ,
    )
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, object]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                TOPICS["host_reading_run_completed"],
                TOPICS["host_reading_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["host_reading_requested"],
            on_message=on_command,
            group_id="host-reading",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(TOPICS["host_reading_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(events: list[tuple[str, dict[str, object]]]) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [TOPICS["host_reading_written"]] * 2 + [
        TOPICS["host_reading_run_completed"]
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[-1][1]["failed"] == 0


def assert_failed_chain(
    events: list[tuple[str, dict[str, object]]], *, verdict: str, outcome: str
) -> None:
    topics = [topic for topic, _ in events]
    assert TOPICS["host_reading_failed"] in topics, topics
    assert topics[-1] == TOPICS["host_reading_run_completed"], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    failed = [body for topic, body in events if topic == TOPICS["host_reading_failed"]]
    assert all(body["verdict"] == verdict for body in failed)
    assert all(body["outcome"] == outcome for body in failed)
    assert events[-1][1]["failed"] == len(failed)


def test_golden_chain_ends_in_success_and_writes_one_row_per_probe(
    tmp_path: Path,
) -> None:
    world = World("memory", tmp_path)
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD.encode()))
    assert_golden_chain(events)
    assert len(ledger.lines) == 2
    assert all(
        "src=host-reading" in line and "verdict=WORKING" in line
        for line in ledger.lines
    )


def test_error_chain_unreadable_source_gives_a_failure_event_and_unknown_row(
    tmp_path: Path,
) -> None:
    world = World("memory", tmp_path)
    assert world.memory is not None
    world.memory.read_error = "injected read failure"
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD.encode()))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert len(ledger.lines) == 2
    assert all("verdict=UNKNOWN" in line for line in ledger.lines)
    assert all("reason=host-read-failed-oserror" in line for line in ledger.lines)


def test_error_chain_append_that_does_not_land_is_a_failure_event(
    tmp_path: Path,
) -> None:
    world = World("memory", tmp_path)
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    events = asyncio.run(_publish(world, ledger, PAYLOAD.encode()))
    assert_failed_chain(events, verdict="WORKING", outcome="read")
    assert all(
        body["ledger"] == "append-failed:refused"
        for topic, body in events
        if topic == TOPICS["host_reading_failed"]
    )
    assert ledger.lines == []


def test_error_chain_bad_payload_is_answered_with_a_typed_failure(
    tmp_path: Path,
) -> None:
    events = asyncio.run(
        _publish(World("memory", tmp_path), InMemoryLedger(), b'{"write": "maybe"}')
    )
    assert [topic for topic, _ in events] == [TOPICS["host_reading_run_failed"]]
    assert "bad-payload" in str(events[0][1]["failure"])


def test_error_chain_undeclared_host_is_a_typed_failure(tmp_path: Path) -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(
        _publish(World("memory", tmp_path), ledger, PAYLOAD.encode(), host="other")
    )
    assert [topic for topic, _ in events] == [TOPICS["host_reading_run_failed"]]
    assert events[0][1]["failure"] == "host-not-declared:other"
    assert ledger.lines == []


def test_a_named_break_reads_not_working_without_failing_the_run(
    tmp_path: Path,
) -> None:
    """NOT WORKING is a reading, so the chain still ends in success with the verdict on the row."""
    world = World("memory", tmp_path)
    world.break_restore_old_name_as_regular_file()
    world.break_plant_refused_file()
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD.encode()))
    assert_golden_chain(events)
    assert [body["verdict"] for _, body in events[:-1]] == ["NOT_WORKING"] * 2
    assert all("verdict=NOT_WORKING" in line for line in ledger.lines)


# -- the ledger-durability probe (OMN-22662) ----------------------------------------------------

DURABILITY_PAYLOAD = json.dumps(
    {"correlation_id": CORRELATION, "probes": ["ledger-durability"], "write": True}
)


def _durability_events(
    tmp_path: Path,
    probe_appender: ProtocolReadingAppender,
    reader: ProtocolLedgerRowSource,
    clock: Clock,
) -> tuple[list[tuple[str, dict[str, object]]], InMemoryLedger]:
    world = World("memory", tmp_path)
    readings = InMemoryLedger()
    handler = HandlerHostReading(
        overlay=durability_overlay(),
        host=LEDGER_HOST,
        files=world.files,
        appender=readings,
        clock=clock,
        environ=world.environ,
        probe_appender=probe_appender,
        durable_reader=reader,
        sleep=clock.sleep,
    )
    events = asyncio.run(
        _publish(world, readings, DURABILITY_PAYLOAD.encode(), handler=handler)
    )
    return events, readings


def test_durability_golden_chain_reads_the_probe_row_back(tmp_path: Path) -> None:
    clock = Clock()
    store = LateStore(clock, lag=3)
    events, readings = _durability_events(tmp_path, store, store, clock)
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["host_reading_written"],
        TOPICS["host_reading_run_completed"],
    ]
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == "WORKING"
    assert events[-1][1]["failed"] == 0
    (row,) = readings.lines
    assert "probe=ledger-durability" in row
    assert "durable=yes" in row


def test_durability_error_chain_read_route_down_is_a_failure_event(
    tmp_path: Path,
) -> None:
    clock = Clock()
    events, readings = _durability_events(
        tmp_path, LateStore(clock, 0), DownReader(), clock
    )
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    (row,) = readings.lines
    assert "verdict=UNKNOWN" in row
    assert "reason=durable-read-failed-read-command-exit-1" in row


def test_durability_named_break_reads_not_working_on_the_row(tmp_path: Path) -> None:
    """The plan's fault: an append that returns with no durable row. A reading, not a failed run."""
    clock = Clock()
    events, readings = _durability_events(
        tmp_path, AcceptAndDrop(), InMemoryLedger(), clock
    )
    assert events[0][1]["verdict"] == "NOT_WORKING"
    assert events[0][1]["reasons"] == ["no-durable-row-by-deadline"]
    (row,) = readings.lines
    assert "verdict=NOT_WORKING" in row
    assert "durable=no" in row


# -- each chain assertion can fail: the named breaks turn it red ------------------------------


def _golden_events(tmp_path: Path) -> list[tuple[str, dict[str, object]]]:
    return asyncio.run(
        _publish(World("memory", tmp_path), InMemoryLedger(), PAYLOAD.encode())
    )


def test_break_dropped_terminal_event_turns_the_golden_chain_red(
    tmp_path: Path,
) -> None:
    with pytest.raises(AssertionError):
        assert_golden_chain(_golden_events(tmp_path)[:-1])


def test_break_wrong_correlation_id_turns_the_golden_chain_red(tmp_path: Path) -> None:
    events = _golden_events(tmp_path)
    events[0] = (events[0][0], {**events[0][1], "correlation_id": "other"})
    with pytest.raises(AssertionError):
        assert_golden_chain(events)


def test_break_success_in_place_of_failure_turns_the_error_chain_red(
    tmp_path: Path,
) -> None:
    with pytest.raises(AssertionError):
        assert_failed_chain(
            _golden_events(tmp_path), verdict="UNKNOWN", outcome="source-unreadable"
        )


def test_contract_declares_the_exact_host_reading_event_topics() -> None:
    assert TOPICS["host_reading_written"] == (
        "onex.evt.omnimarket.host-reading-written.v1"
    )
    assert TOPICS["host_reading_failed"] == (
        "onex.evt.omnimarket.host-reading-failed.v1"
    )
    assert TOPICS["host_reading_run_completed"] == (
        "onex.evt.omnimarket.host-reading-run-completed.v1"
    )
    assert TOPICS["host_reading_run_failed"] == (
        "onex.evt.omnimarket.host-reading-run-failed.v1"
    )
