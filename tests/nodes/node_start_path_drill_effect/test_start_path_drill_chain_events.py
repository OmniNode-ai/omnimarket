# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the start-path drill (plan section 3.2, OMN-23306).

Publish the input event on an in-memory bus and assert every event it produces, in order, under one correlation
id, ending in the run-completed event; inject a defined fault and assert the typed failure event and the reading
on the ledger row. The last test shows the golden assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_start_path_drill_effect.handlers.handler_start_path_drill import (
    CONTRACT,
    answer_command,
    load_topics,
)
from tests.nodes.node_start_path_drill_effect.drill_support import World

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "start_path_drill_written",
        "start_path_drill_failed",
        "start_path_drill_run_completed",
        "start_path_drill_run_failed",
    )
]
CORRELATION = "chain-1"
PAYLOAD = json.dumps({"correlation_id": CORRELATION, "write": True}).encode()
TERMINAL_EVENT = "onex.evt.omnimarket.start-path-drill-run-completed.v1"


async def _publish(
    world: World, payload: bytes, *, write: bool = True
) -> list[tuple[str, dict[str, object]]]:
    handler = world.handler(write=write)
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
                TOPICS["start_path_drill_run_completed"],
                TOPICS["start_path_drill_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["start_path_drill_requested"],
            on_message=on_command,
            group_id="start-path-drill",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(TOPICS["start_path_drill_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(events: list[tuple[str, dict[str, object]]]) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["start_path_drill_written"],
        TOPICS["start_path_drill_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == "WORKING"
    assert events[0][1]["ledger"] == "written"
    assert (events[0][1]["refused"], events[0][1]["admitted"]) == (15, 0)
    assert events[-1][1]["failed"] == 0


def test_start_path_drill_golden_chain_ends_in_success_and_writes_one_working_row() -> (
    None
):
    world = World()
    assert_golden_chain(asyncio.run(_publish(world, PAYLOAD)))
    assert len(world.reading_ledger.lines) == 1
    assert "verdict=WORKING" in world.reading_ledger.lines[0]
    assert len(world.log_ledger.lines) == 30


def test_start_path_drill_golden_chain_ends_in_the_contract_terminal_event() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL_EVENT
    events = asyncio.run(_publish(World(), PAYLOAD))
    assert events[-1][0] == TERMINAL_EVENT


def test_start_path_drill_error_chain_a_bypassing_start_is_a_written_not_working_row() -> (
    None
):
    """The named break: the gate admits a planted start. The drill ran and its row says NOT_WORKING."""
    world = World()
    world.break_gate_admits_everything()
    events = asyncio.run(_publish(world, PAYLOAD))
    assert [t for t, _ in events] == [
        TOPICS["start_path_drill_written"],
        TOPICS["start_path_drill_run_completed"],
    ]
    assert events[0][1]["verdict"] == "NOT_WORKING"
    assert events[0][1]["admitted"] == 15
    assert "verdict=NOT_WORKING" in world.reading_ledger.lines[0]


def test_start_path_drill_error_chain_append_that_does_not_land_is_a_failure_event() -> (
    None
):
    world = World()
    world.reading_ledger.append_status = "refused"
    events = asyncio.run(_publish(world, PAYLOAD))
    assert [t for t, _ in events] == [
        TOPICS["start_path_drill_failed"],
        TOPICS["start_path_drill_run_completed"],
    ]
    assert events[0][1]["ledger"] == "append-failed:refused"
    assert events[-1][1]["failed"] == 1
    assert world.reading_ledger.lines == []


def test_start_path_drill_error_chain_bad_payload_is_answered_with_a_typed_failure() -> (
    None
):
    events = asyncio.run(_publish(World(), b'{"write": "maybe"}'))
    assert [t for t, _ in events] == [TOPICS["start_path_drill_run_failed"]]
    assert "bad-payload" in str(events[0][1]["failure"])


def test_start_path_drill_error_chain_writing_run_without_appender_is_a_typed_failure() -> (
    None
):
    events = asyncio.run(_publish(World(), PAYLOAD, write=False))
    assert [t for t, _ in events] == [TOPICS["start_path_drill_run_failed"]]
    assert events[0][1]["failure"] == "no-appender-for-a-writing-run"


def test_start_path_drill_named_break_golden_assertion_turns_red_when_a_path_is_left_out() -> (
    None
):
    world = World()
    world.break_path_left_out_of_the_census("lane_runner")
    events = asyncio.run(_publish(world, PAYLOAD))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)
