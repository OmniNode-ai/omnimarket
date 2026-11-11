# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the broker topic delete (OMN-23879).

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

from omnimarket.nodes.node_broker_topic_delete_effect.handlers.handler_broker_topic_delete import (
    answer_command,
    load_topics,
)
from tests.nodes.node_broker_topic_delete_effect.delete_support import TOPICS as DATA
from tests.nodes.node_broker_topic_delete_effect.delete_support import World

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "broker_topic_delete_reported",
        "broker_topic_delete_refused",
        "broker_topic_delete_run_completed",
        "broker_topic_delete_run_failed",
    )
]
CORRELATION = "delete-test-1"


async def _publish(world: World, payload: bytes) -> list[tuple[str, dict[str, object]]]:
    handler = world.handler()
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
                TOPICS["broker_topic_delete_run_completed"],
                TOPICS["broker_topic_delete_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["broker_topic_delete_requested"],
            on_message=on_command,
            group_id="topic-delete",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["broker_topic_delete_requested"], key=None, value=payload
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def _payload(world: World, *keys: str, approval_ref: str | None = None) -> bytes:
    return world.command(*keys, approval_ref=approval_ref).model_dump_json().encode()


def assert_golden_chain(events: list[tuple[str, dict[str, object]]]) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["broker_topic_delete_reported"],
        TOPICS["broker_topic_delete_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["status"] == "report_only"
    entries = events[0][1]["entries"]
    assert isinstance(entries, list)
    assert [(entry["name"], entry["status"]) for entry in entries] == [
        (DATA["clean"], "would_delete"),
        (DATA["group"], "refused"),
    ]
    assert events[0][1]["deletes_issued"] == 0
    assert events[-1][1]["failed"] == 0


def assert_refused_chain(
    events: list[tuple[str, dict[str, object]]], *, refusal_reasons: list[str]
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["broker_topic_delete_refused"],
        TOPICS["broker_topic_delete_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["status"] == "refused"
    assert events[0][1]["refusal_reasons"] == refusal_reasons
    assert events[-1][1]["failed"] == 1


def test_golden_chain_reports_per_topic_and_completes_under_one_correlation_id() -> (
    None
):
    world = World()
    events = asyncio.run(_publish(world, _payload(world, "clean", "group")))
    assert_golden_chain(events)
    assert world.admin.calls == []


def test_golden_chain_enforced_deletes_exactly_the_clean_topic() -> None:
    world = World(enforce=True)
    events = asyncio.run(
        _publish(world, _payload(world, "clean", "group", approval_ref="ledger-row-1"))
    )
    body = events[0][1]
    assert body["status"] == "enforced"
    assert body["deletes_issued"] == 1
    assert world.admin.calls == [(DATA["clean"],)]
    assert events[-1][1]["failed"] == 0


def test_error_chain_enforce_without_approval_is_refused_and_completed_failed() -> None:
    world = World(enforce=True)
    events = asyncio.run(_publish(world, _payload(world, "clean")))
    assert_refused_chain(events, refusal_reasons=["approval_missing"])
    assert world.admin.calls == []


def test_error_chain_an_unreadable_broker_is_refused_and_completed_failed() -> None:
    world = World()
    world.break_broker_unreadable()
    events = asyncio.run(_publish(world, _payload(world, "clean", "group")))
    assert_refused_chain(
        events, refusal_reasons=["broker_unreadable:broker-unreadable"]
    )


def test_error_chain_a_failed_delete_completes_failed() -> None:
    world = World(enforce=True)
    world.break_delete("clean", "TOPIC_AUTHORIZATION_FAILED")
    events = asyncio.run(
        _publish(world, _payload(world, "clean", approval_ref="ledger-row-1"))
    )
    entries = events[0][1]["entries"]
    assert isinstance(entries, list)
    assert entries[0]["status"] == "failed"
    assert entries[0]["reasons"] == ["delete_failed:TOPIC_AUTHORIZATION_FAILED"]
    assert events[-1][1]["failed"] == 1


def test_error_chain_a_bad_payload_is_answered_with_a_typed_failure() -> None:
    events = asyncio.run(_publish(World(), b'{"topics": []}'))
    assert [topic for topic, _ in events] == [TOPICS["broker_topic_delete_run_failed"]]
    assert str(events[0][1]["failure"]).startswith("bad-payload")


def test_named_break_the_golden_assertion_turns_red_when_the_broker_is_unreadable() -> (
    None
):
    world = World()
    world.break_broker_unreadable()
    events = asyncio.run(_publish(world, _payload(world, "clean", "group")))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)


def test_the_terminal_event_of_the_contract_is_the_run_completed_topic() -> None:
    """The contract's terminal event is the topic the golden chain ends on."""
    assert (
        TOPICS["broker_topic_delete_run_completed"]
        == "onex.evt.omnimarket.broker-topic-delete-run-completed.v1"
    )
