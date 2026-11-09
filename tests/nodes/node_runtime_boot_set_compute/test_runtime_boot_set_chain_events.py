# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the boot set judgment (one-runtime plan T28, OMN-23119).

Publish the judge request on the in-memory bus and assert the event it produces under one correlation id; inject
today's start and assert the violated event; send a payload that is no request and assert the typed failure. The last
block shows each chain assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_runtime_boot_set_compute.handlers.handler_runtime_boot_set import (
    HandlerRuntimeBootSet,
    answer_command,
)
from tests.nodes.node_runtime_boot_set_compute.boot_set_support import (
    CORRELATION,
    declaration,
    declared_boot,
    load_topics,
    request,
    todays_boot,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
RESULT_TOPICS = [
    TOPICS[role]
    for role in ("boot_set_judged", "boot_set_violated", "boot_set_judge_failed")
]
Events = list[tuple[str, dict[str, Any]]]


async def _publish(payload: bytes, handler: HandlerRuntimeBootSet) -> Events:
    bus = EventBusInmemory()
    seen: Events = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["boot_set_judge_requested"],
            on_message=on_command,
            group_id="boot-set",
        )
        for topic in RESULT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(TOPICS["boot_set_judge_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def run(payload: bytes, handler: HandlerRuntimeBootSet | None = None) -> Events:
    return asyncio.run(_publish(payload, handler or HandlerRuntimeBootSet()))


def assert_golden_chain(events: Events) -> None:
    assert [topic for topic, _ in events] == [TOPICS["boot_set_judged"]], events
    body = events[0][1]
    assert body["correlation_id"] == CORRELATION
    assert body["status"] == "PASS"
    assert body["rules"] == []
    assert body["contracts_over_declared"] == 0


def assert_error_chain(events: Events, rules: list[str]) -> None:
    assert [topic for topic, _ in events] == [TOPICS["boot_set_violated"]], events
    body = events[0][1]
    assert body["correlation_id"] == CORRELATION
    assert body["status"] == "FAIL"
    assert body["rules"] == rules
    assert body["contracts_over_declared"] > 0


def golden() -> bytes:
    return request(declared_boot(), declaration()).model_dump_json().encode()


def todays_start() -> bytes:
    return request(todays_boot(), declaration()).model_dump_json().encode()


def test_golden_chain_a_boot_inside_the_declared_set_ends_in_one_judged_event() -> None:
    assert_golden_chain(run(golden()))


def test_error_chain_todays_start_against_the_declared_set_ends_in_a_violated_event() -> (
    None
):
    events = run(todays_start())
    assert_error_chain(
        events, ["LOADED_OUTSIDE_DECLARED_SET", "SUBSCRIBED_OUTSIDE_DECLARED_SET"]
    )


def test_error_chain_todays_start_with_no_declared_set_ends_in_a_violated_event() -> (
    None
):
    payload = request(todays_boot(), None).model_dump_json().encode()
    assert_error_chain(run(payload), ["NO_DECLARED_SET"])


def test_error_chain_bad_payload_is_answered_with_a_typed_failure() -> None:
    events = run(json.dumps({"deployment": "dev"}).encode())
    assert [topic for topic, _ in events] == [TOPICS["boot_set_judge_failed"]]
    assert str(events[0][1]["failure"]).startswith("bad-payload:")


# -- each chain assertion can fail: the named breaks turn it red ------------------------------


class _IgnoresExcess(HandlerRuntimeBootSet):
    """Break: the judgment stops counting contracts loaded outside the declared set."""

    def handle(self, request):  # type: ignore[no-untyped-def]
        verdict = super().handle(request)
        return verdict.model_copy(
            update={"status": "PASS", "rules": (), "contracts_over_declared": 0}
        )


def test_break_ignored_excess_turns_the_error_chain_red() -> None:
    events = run(todays_start(), _IgnoresExcess())
    with pytest.raises(AssertionError):
        assert_error_chain(
            events, ["LOADED_OUTSIDE_DECLARED_SET", "SUBSCRIBED_OUTSIDE_DECLARED_SET"]
        )


def test_break_todays_start_in_place_of_a_declared_boot_turns_the_golden_chain_red() -> (
    None
):
    with pytest.raises(AssertionError):
        assert_golden_chain(run(todays_start()))


def test_break_dropped_result_event_turns_the_golden_chain_red() -> None:
    with pytest.raises(AssertionError):
        assert_golden_chain(run(golden())[:-1])


def test_break_wrong_rule_named_turns_the_error_chain_red() -> None:
    with pytest.raises(AssertionError):
        assert_error_chain(run(todays_start()), ["NO_DECLARED_SET"])
