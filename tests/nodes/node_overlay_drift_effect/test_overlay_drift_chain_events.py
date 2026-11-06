# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the overlay drift read (plan section 3.2, OMN-22660).

Publish the input event and assert every event it produces, in order, under one correlation id,
ending in the run-completed event; inject a defined fault and assert the typed failure event and
the reading on the ledger row. The last tests show the chain assertions turn red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_overlay_drift_effect.handlers.handler_overlay_drift import (
    CONTRACT,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_overlay_drift_effect.overlay_drift_support import PLANTED, World

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "overlay_drift_written",
        "overlay_drift_failed",
        "overlay_drift_run_completed",
        "overlay_drift_run_failed",
    )
]
CORRELATION = "chain-1"
PAYLOAD = json.dumps({"correlation_id": CORRELATION, "write": True}).encode()


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
                TOPICS["overlay_drift_run_completed"],
                TOPICS["overlay_drift_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["overlay_drift_requested"],
            on_message=on_command,
            group_id="overlay-drift",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(TOPICS["overlay_drift_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(
    events: list[tuple[str, dict[str, object]]], verdict: str = "WORKING"
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["overlay_drift_written"],
        TOPICS["overlay_drift_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == verdict
    assert events[0][1]["ledger"] == "written"
    assert events[-1][1]["failed"] == 0


def assert_failed_chain(
    events: list[tuple[str, dict[str, object]]], *, verdict: str, outcome: str
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["overlay_drift_failed"],
        TOPICS["overlay_drift_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == verdict
    assert events[0][1]["outcome"] == outcome
    assert events[-1][1]["failed"] == 1


def test_golden_chain_ends_in_success_and_writes_one_working_row(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(World("memory", tmp_path), ledger, PAYLOAD))
    assert_golden_chain(events)
    assert len(ledger.lines) == 1
    row = ledger.lines[0]
    assert "lane=overlay-drift" in row
    assert "src=overlay-drift" in row
    assert "verdict=WORKING" in row
    assert "checked=2" in row
    assert "accepted=2" in row
    assert "rejected=0" in row


# The contract's terminal event, spelled out so a renamed topic turns this test red.
TERMINAL_EVENT = "onex.evt.omnimarket.overlay-drift-run-completed.v1"


def test_golden_chain_ends_in_the_contract_terminal_event(tmp_path: Path) -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL_EVENT
    events = asyncio.run(_publish(World("memory", tmp_path), InMemoryLedger(), PAYLOAD))
    assert events[-1][0] == TERMINAL_EVENT


def test_error_chain_unreadable_pins_gives_a_failure_event_and_unknown_row(
    tmp_path: Path,
) -> None:
    world = World("memory", tmp_path)
    world.break_unreadable_pins()
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert len(ledger.lines) == 1
    assert "verdict=UNKNOWN" in ledger.lines[0]
    assert "reason=home-unreadable-pins-line-" in ledger.lines[0]


def test_error_chain_absent_runtime_gives_a_failure_event_and_unknown_row(
    tmp_path: Path,
) -> None:
    world = World("memory", tmp_path)
    world.break_runtime_absent()
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert "reason=runtime-unreadable-runtime-interpreter-absent" in ledger.lines[0]


def test_error_chain_append_that_does_not_land_is_a_failure_event(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    events = asyncio.run(_publish(World("memory", tmp_path), ledger, PAYLOAD))
    assert_failed_chain(events, verdict="WORKING", outcome="read")
    assert events[0][1]["ledger"] == "append-failed:refused"
    assert ledger.lines == []


def test_error_chain_bad_payload_is_answered_with_a_typed_failure(
    tmp_path: Path,
) -> None:
    events = asyncio.run(
        _publish(World("memory", tmp_path), InMemoryLedger(), b'{"write": "maybe"}')
    )
    assert [topic for topic, _ in events] == [TOPICS["overlay_drift_run_failed"]]
    assert "bad-payload" in str(events[0][1]["failure"])


def test_error_chain_writing_run_without_appender_is_a_typed_failure(
    tmp_path: Path,
) -> None:
    events = asyncio.run(_publish(World("memory", tmp_path), None, PAYLOAD))
    assert [topic for topic, _ in events] == [TOPICS["overlay_drift_run_failed"]]
    assert events[0][1]["failure"] == "no-appender-for-a-writing-run"


def test_named_break_planted_key_reads_not_working_without_failing_the_run(
    tmp_path: Path,
) -> None:
    """NOT WORKING is a reading: the chain still ends in success with the verdict on the row."""
    world = World("memory", tmp_path)
    world.break_plant_rejected_key()
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert_golden_chain(events, verdict="NOT_WORKING")
    row = ledger.lines[0]
    assert "verdict=NOT_WORKING" in row
    assert "rejected=1" in row
    assert f"failing={PLANTED}:rejected:rung_order" in row


def test_named_break_turns_the_golden_assertion_red(tmp_path: Path) -> None:
    """The golden assertion is not vacuous: with the planted key it fails."""
    world = World("memory", tmp_path)
    world.break_plant_rejected_key()
    events = asyncio.run(_publish(world, InMemoryLedger(), PAYLOAD))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)
