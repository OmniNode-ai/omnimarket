# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the broker load reading (plan section 3.2, OMN-22800).

Publish the input event and assert every event it produces, in order, under one correlation id, ending in
the run-completed event; inject a defined fault and assert the typed failure event and the reading on the
ledger row. The last test shows the golden assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_broker_load_reading_effect.handlers.handler_broker_load import (
    CONTRACT,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_broker_load_reading_effect.broker_load_support import World

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "broker_load_written",
        "broker_load_failed",
        "broker_load_run_completed",
        "broker_load_run_failed",
    )
]
CORRELATION = "chain-1"
PAYLOAD = json.dumps({"correlation_id": CORRELATION, "write": True}).encode()
TERMINAL_EVENT = "onex.evt.omnimarket.broker-load-run-completed.v1"


async def _publish(
    world: World, ledger: InMemoryLedger | None, payload: bytes
) -> list[tuple[str, dict[str, object]]]:
    handler = world.handler(ledger)
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
                TOPICS["broker_load_run_completed"],
                TOPICS["broker_load_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["broker_load_requested"],
            on_message=on_command,
            group_id="broker-load",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(TOPICS["broker_load_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(events: list[tuple[str, dict[str, object]]]) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["broker_load_written"],
        TOPICS["broker_load_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == "WORKING"
    assert events[0][1]["ledger"] == "written"
    assert events[-1][1]["failed"] == 0


def assert_failed_chain(
    events: list[tuple[str, dict[str, object]]], *, verdict: str, outcome: str
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["broker_load_failed"],
        TOPICS["broker_load_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == verdict
    assert events[0][1]["outcome"] == outcome
    assert events[-1][1]["failed"] == 1


def test_golden_chain_ends_in_success_and_writes_one_working_row() -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(World(), ledger, PAYLOAD))
    assert_golden_chain(events)
    assert len(ledger.lines) == 1
    row = ledger.lines[0]
    assert "lane=broker-load" in row
    assert "verdict=WORKING" in row
    assert "partitions_per_shard=320" in row


def test_golden_chain_ends_in_the_contract_terminal_event() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL_EVENT
    events = asyncio.run(_publish(World(), InMemoryLedger(), PAYLOAD))
    assert events[-1][0] == TERMINAL_EVENT


def test_error_chain_unreadable_metrics_gives_a_failure_event_and_unknown_row() -> None:
    world = World()
    world.break_metrics_unreadable()
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert "verdict=UNKNOWN" in ledger.lines[0]
    assert "reason=metrics-unreadable-metrics-unreadable" in ledger.lines[0]


def test_error_chain_unreadable_consumer_gives_a_failure_event_and_unknown_row() -> (
    None
):
    world = World()
    world.break_consumer_transport_unreadable()
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert "reason=consumer-unreadable-oserror" in ledger.lines[0]


def test_error_chain_the_overloaded_shard_is_a_written_not_working_row() -> None:
    """The named break: partitions over the bar. The read succeeds and the row says NOT_WORKING."""
    world = World()
    world.break_partitions_over_the_bar()
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert [topic for topic, _ in events] == [
        TOPICS["broker_load_written"],
        TOPICS["broker_load_run_completed"],
    ]
    assert events[0][1]["verdict"] == "NOT_WORKING"
    assert events[0][1]["partitions_per_shard"] == 2894
    assert "verdict=NOT_WORKING" in ledger.lines[0]


def test_error_chain_append_that_does_not_land_is_a_failure_event() -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    events = asyncio.run(_publish(World(), ledger, PAYLOAD))
    assert_failed_chain(events, verdict="WORKING", outcome="read")
    assert events[0][1]["ledger"] == "append-failed:refused"
    assert ledger.lines == []


def test_error_chain_bad_payload_is_answered_with_a_typed_failure() -> None:
    events = asyncio.run(_publish(World(), InMemoryLedger(), b'{"write": "maybe"}'))
    assert [topic for topic, _ in events] == [TOPICS["broker_load_run_failed"]]
    assert "bad-payload" in str(events[0][1]["failure"])


def test_error_chain_writing_run_without_appender_is_a_typed_failure() -> None:
    events = asyncio.run(_publish(World(), None, PAYLOAD))
    assert [topic for topic, _ in events] == [TOPICS["broker_load_run_failed"]]
    assert events[0][1]["failure"] == "no-appender-for-a-writing-run"


def test_named_break_golden_assertion_turns_red_when_the_shard_is_overloaded() -> None:
    world = World()
    world.break_partitions_over_the_bar()
    events = asyncio.run(_publish(world, InMemoryLedger(), PAYLOAD))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)
