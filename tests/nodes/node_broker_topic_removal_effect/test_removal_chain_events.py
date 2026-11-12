# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the broker topic removal (OMN-23697).

Publish the command on its requested topic and assert every event it produces, in order, under one
correlation id, ending in the run-completed event; inject a defined fault and assert the typed
refusal or failure event. The last test shows the golden assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_broker_topic_removal_effect.handlers.handler_broker_topic_removal import (
    answer_command,
    load_topics,
)
from tests.nodes.node_broker_topic_removal_effect.removal_support import (
    TOPICS as DATA_TOPICS,
)
from tests.nodes.node_broker_topic_removal_effect.removal_support import (
    World,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "broker_topic_removal_planned",
        "broker_topic_removal_refused",
        "broker_topic_removal_run_completed",
        "broker_topic_removal_run_failed",
    )
]
CORRELATION = "removal-chain-1"
DRY_PAYLOAD = json.dumps({"correlation_id": CORRELATION, "mode": "dry_run"}).encode()
EXECUTE_PAYLOAD = json.dumps(
    {"correlation_id": CORRELATION, "mode": "execute"}
).encode()


async def _publish(
    world: World, payload: bytes, *, remover: bool = False
) -> list[tuple[str, dict[str, object]]]:
    handler = world.handler(remover=remover)
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
                TOPICS["broker_topic_removal_run_completed"],
                TOPICS["broker_topic_removal_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["broker_topic_removal_requested"],
            on_message=on_command,
            group_id="topic-removal",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["broker_topic_removal_requested"], key=None, value=payload
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(events: list[tuple[str, dict[str, object]]]) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["broker_topic_removal_planned"],
        TOPICS["broker_topic_removal_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["status"] == "planned"
    candidates = events[0][1]["candidates"]
    assert isinstance(candidates, list)
    assert [candidate["name"] for candidate in candidates] == [DATA_TOPICS["d"]]
    assert events[0][1]["partitions_after"] == 5
    assert events[-1][1]["failed"] == 0


def assert_refused_chain(
    events: list[tuple[str, dict[str, object]]], *, refusal_reasons: list[str]
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["broker_topic_removal_refused"],
        TOPICS["broker_topic_removal_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["status"] == "refused"
    assert events[0][1]["refusal_reasons"] == refusal_reasons
    assert events[-1][1]["failed"] == 1


def test_golden_chain_plans_and_completes_under_one_correlation_id() -> None:
    events = asyncio.run(_publish(World(), DRY_PAYLOAD))
    assert_golden_chain(events)


def test_error_chain_execute_without_approval_is_refused_and_completed_failed() -> None:
    world = World()
    world.enable_execute()
    events = asyncio.run(_publish(world, EXECUTE_PAYLOAD, remover=True))
    assert_refused_chain(events, refusal_reasons=["approval_missing"])


def test_error_chain_an_unreadable_inventory_is_refused_and_completed_failed() -> None:
    world = World()
    world.break_inventory_unreadable()
    events = asyncio.run(_publish(world, DRY_PAYLOAD))
    assert_refused_chain(
        events, refusal_reasons=["inventory_unreadable:inventory-unreadable"]
    )


def test_error_chain_a_bad_payload_is_answered_with_a_typed_failure() -> None:
    events = asyncio.run(_publish(World(), b'{"mode": "maybe"}'))
    assert [topic for topic, _ in events] == [TOPICS["broker_topic_removal_run_failed"]]
    assert str(events[0][1]["failure"]).startswith("bad-payload")


def test_named_break_the_golden_assertion_turns_red_when_the_inventory_is_unreadable() -> (
    None
):
    world = World()
    world.break_inventory_unreadable()
    events = asyncio.run(_publish(world, DRY_PAYLOAD))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)
