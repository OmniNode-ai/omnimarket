# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the broker authentication refusal reading (OMN-19044, AC-4).

Publish the input event and assert every event it produces, in order, under one correlation id, ending in
the run-completed event; inject a defined fault and assert the typed failure event and the reading on the
ledger row. The last two tests show the reading goes red on a named break: a reader that is blind to a
refusal the broker logged reads WORKING where the same log reads NOT_WORKING, so the assertion fails.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_broker_auth_refusal_reading_effect.handlers.handler_broker_auth_refusal import (
    CONTRACT,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_broker_auth_refusal_reading_effect.auth_refusal_support import (
    World,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "broker_auth_refusal_written",
        "broker_auth_refusal_failed",
        "broker_auth_refusal_run_completed",
        "broker_auth_refusal_run_failed",
    )
]
CORRELATION = "chain-1"
PAYLOAD = json.dumps({"correlation_id": CORRELATION, "write": True}).encode()
TERMINAL_EVENT = "onex.evt.omnimarket.broker-auth-refusal-run-completed.v1"


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
                TOPICS["broker_auth_refusal_run_completed"],
                TOPICS["broker_auth_refusal_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["broker_auth_refusal_requested"],
            on_message=on_command,
            group_id="broker-auth-refusal",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["broker_auth_refusal_requested"], key=None, value=payload
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(events: list[tuple[str, dict[str, object]]]) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["broker_auth_refusal_written"],
        TOPICS["broker_auth_refusal_run_completed"],
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
        TOPICS["broker_auth_refusal_failed"],
        TOPICS["broker_auth_refusal_run_completed"],
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
    assert "verdict=WORKING" in ledger.lines[0]
    assert "refusals=0" in ledger.lines[0]


def test_golden_chain_ends_in_the_contract_terminal_event() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL_EVENT
    events = asyncio.run(_publish(World(), InMemoryLedger(), PAYLOAD))
    assert events[-1][0] == TERMINAL_EVENT


def test_error_chain_unreadable_log_gives_a_failure_event_and_unknown_row() -> None:
    world = World()
    world.break_log_unreadable()
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert "verdict=UNKNOWN" in ledger.lines[0]
    assert "reason=log-unreadable-log-unreadable" in ledger.lines[0]


def test_error_chain_the_refused_client_is_a_written_not_working_row() -> None:
    """The named break: a client refused in a burst. The read succeeds and the row says NOT_WORKING."""
    world = World()
    world.break_client_refused_in_a_burst()
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert [topic for topic, _ in events] == [
        TOPICS["broker_auth_refusal_written"],
        TOPICS["broker_auth_refusal_run_completed"],
    ]
    assert events[0][1]["verdict"] == "NOT_WORKING"
    assert events[0][1]["refusals"] == 3
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
    assert [topic for topic, _ in events] == [TOPICS["broker_auth_refusal_run_failed"]]
    assert "bad-payload" in str(events[0][1]["failure"])


def test_error_chain_writing_run_without_appender_is_a_typed_failure() -> None:
    events = asyncio.run(_publish(World(), None, PAYLOAD))
    assert [topic for topic, _ in events] == [TOPICS["broker_auth_refusal_run_failed"]]
    assert events[0][1]["failure"] == "no-appender-for-a-writing-run"


def test_named_break_golden_assertion_turns_red_when_a_client_is_refused() -> None:
    world = World()
    world.break_client_refused_in_a_burst()
    events = asyncio.run(_publish(world, InMemoryLedger(), PAYLOAD))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)


def test_named_break_a_blind_reader_reads_working_where_the_log_holds_refusals() -> (
    None
):
    """Control: the broker logged three refusals; with the reader blind the reading is a false WORKING.

    The assertion a healthy reading must satisfy (the refusals are seen) fails under the break, so the
    reading can fail for real: it is the reader being blind that turns it green, not the broker being clean.
    """
    seeing = World()
    seeing.break_client_refused_in_a_burst()
    seen = asyncio.run(_publish(seeing, InMemoryLedger(), PAYLOAD))
    assert seen[0][1]["verdict"] == "NOT_WORKING"

    blind = World()
    blind.break_reader_is_blind()
    unseen = asyncio.run(_publish(blind, InMemoryLedger(), PAYLOAD))
    assert unseen[0][1]["verdict"] == "WORKING"
    with pytest.raises(AssertionError):
        assert unseen[0][1]["verdict"] == "NOT_WORKING"
