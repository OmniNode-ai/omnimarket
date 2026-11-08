# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain event test of the host table copies drift read (OMN-22809).

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

from omnimarket.nodes.node_host_table_copies_drift_compute.handlers.handler_host_table_copies_drift import (
    answer_command,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_host_table_copies_drift_compute.drift_support import World

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_table_copies_drift_compute/contract.yaml"
)
TOPICS: dict[str, str] = dict(
    yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["topics"]
)
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "host_table_copies_drift_written",
        "host_table_copies_drift_failed",
        "host_table_copies_drift_run_completed",
        "host_table_copies_drift_run_failed",
    )
]
CORRELATION = "chain-1"
PAYLOAD = json.dumps({"correlation_id": CORRELATION, "write": True}).encode()
# The contract's terminal event, spelled out so a renamed topic turns this test red.
TERMINAL_EVENT = "onex.evt.omnimarket.host-table-copies-drift-run-completed.v1"


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
                TOPICS["host_table_copies_drift_run_completed"],
                TOPICS["host_table_copies_drift_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["host_table_copies_drift_requested"],
            on_message=on_command,
            group_id="host-table-copies-drift",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["host_table_copies_drift_requested"], key=None, value=payload
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
        TOPICS["host_table_copies_drift_written"],
        TOPICS["host_table_copies_drift_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == verdict
    assert events[0][1]["ledger"] == "written"
    assert events[-1][1]["failed"] == 0


def test_golden_chain_identical_copies_write_one_working_row() -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(publish(World(), ledger, PAYLOAD))
    assert_golden_chain(events)
    assert len(ledger.lines) == 1
    row = ledger.lines[0]
    assert "lane=host-table-copies-drift" in row
    assert "src=host-table-copies-drift" in row
    assert "verdict=WORKING" in row
    assert "copies=packaged,config,overlays" in row
    assert "hosts=2" in row
    assert "differing_fields=0" in row
    assert "differing=none" in row


def test_golden_chain_ends_in_the_contract_terminal_event() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL_EVENT
    events = asyncio.run(publish(World(), InMemoryLedger(), PAYLOAD))
    assert events[-1][0] == TERMINAL_EVENT


def test_golden_chain_a_changed_cap_is_a_reading_not_a_failure() -> None:
    """NOT WORKING is a reading: the chain still ends in success with the verdict on the row."""
    world = World()
    world.break_change_a_cap()
    ledger = InMemoryLedger()
    events = asyncio.run(publish(world, ledger, PAYLOAD))
    assert_golden_chain(events, verdict="NOT_WORKING")
    assert "differing=host-a.cap=packaged:3+config:6+overlays:3" in ledger.lines[0]
    assert events[0][1]["differences"] == [
        {
            "host": "host-a",
            "field": "cap",
            "values": [
                {"copy_name": "packaged", "value": "3"},
                {"copy_name": "config", "value": "6"},
                {"copy_name": "overlays", "value": "3"},
            ],
        }
    ]


def test_named_break_turns_the_golden_assertion_red() -> None:
    """The golden assertion is not vacuous: with a changed cap it fails."""
    world = World()
    world.break_change_a_cap()
    events = asyncio.run(publish(world, InMemoryLedger(), PAYLOAD))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)
