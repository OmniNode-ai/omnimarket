# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain event test of the overlay consumer join check (OMN-23150).

Publish the input event and assert every event it produces, in order, under one correlation id,
ending in the run-completed event; the last test shows the assertion turns red on a named break.
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

from omnimarket.nodes.node_overlay_drift_compute.handlers.handler_overlay_drift_compute import (
    answer_command,
)
from omnimarket.nodes.node_overlay_drift_compute.tests.join_support import NODE, World
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

pytestmark = pytest.mark.unit

CONTRACT = NODE / "contract.yaml"
TOPICS: dict[str, str] = dict(
    yaml.safe_load(Path(CONTRACT).read_text(encoding="utf-8"))["topics"]
)
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "overlay_consumer_join_written",
        "overlay_consumer_join_failed",
        "overlay_consumer_join_run_completed",
        "overlay_consumer_join_run_failed",
    )
]
CORRELATION = "chain-1"
PAYLOAD = json.dumps({"correlation_id": CORRELATION, "write": True}).encode()
# The contract's terminal event, spelled out so a renamed topic turns this test red.
TERMINAL_EVENT = "onex.evt.omnimarket.overlay-consumer-join-run-completed.v1"


async def publish(
    world: World, ledger: InMemoryLedger | None, payload: bytes
) -> list[tuple[str, dict[str, Any]]]:
    handler = world.handler(ledger)
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                TOPICS["overlay_consumer_join_run_completed"],
                TOPICS["overlay_consumer_join_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["overlay_consumer_join_requested"],
            on_message=on_command,
            group_id="overlay-consumer-join",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["overlay_consumer_join_requested"], key=None, value=payload
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(
    events: list[tuple[str, dict[str, Any]]], verdict: str = "WORKING"
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["overlay_consumer_join_written"],
        TOPICS["overlay_consumer_join_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == verdict
    assert events[0][1]["ledger"] == "written"
    assert events[-1][1]["failed"] == 0


def test_golden_chain_matching_consumer_writes_one_working_row() -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(publish(World(), ledger, PAYLOAD))
    assert_golden_chain(events)
    assert len(ledger.lines) == 1
    row = ledger.lines[0]
    assert "lane=overlay-consumer-join" in row
    assert "src=overlay-consumer-join" in row
    assert "verdict=WORKING" in row
    assert "consumers=1" in row
    assert "mismatched=0" in row
    assert "park_lists=0" in row
    assert "differing=none" in row


def test_golden_chain_ends_in_the_contract_terminal_event() -> None:
    contract = yaml.safe_load(Path(CONTRACT).read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL_EVENT
    events = asyncio.run(publish(World(), InMemoryLedger(), PAYLOAD))
    assert events[-1][0] == TERMINAL_EVENT


def test_golden_chain_a_planted_floor_is_a_reading_not_a_failure() -> None:
    """NOT WORKING is a reading: the chain still ends in success with the verdict on the row."""
    world = World()
    world.break_plant_a_floor_of_5_gib_in_the_hook()
    ledger = InMemoryLedger()
    events = asyncio.run(publish(world, ledger, PAYLOAD))
    assert_golden_chain(events, verdict="NOT_WORKING")
    assert events[0][1]["mismatches"][0]["consumer"] == "example-min-free"
    assert events[0][1]["mismatches"][0]["consumer_value"] == "5"
    assert "mismatched=1" in ledger.lines[0]


def test_golden_chain_a_park_list_outside_an_overlay_is_a_reading_not_a_failure() -> (
    None
):
    world = World()
    world.break_add_a_park_list_to_the_hook()
    events = asyncio.run(publish(world, InMemoryLedger(), PAYLOAD))
    assert_golden_chain(events, verdict="NOT_WORKING")
    assert events[0][1]["park_lists"][0]["scan"] == "example-hook-park-list"


def test_named_break_turns_the_golden_assertion_red() -> None:
    """The golden assertion is not vacuous: with a planted floor it fails."""
    world = World()
    world.break_plant_a_floor_of_5_gib_in_the_hook()
    events = asyncio.run(publish(world, InMemoryLedger(), PAYLOAD))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)
