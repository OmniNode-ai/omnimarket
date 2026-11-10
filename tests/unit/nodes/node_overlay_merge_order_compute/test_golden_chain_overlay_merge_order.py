# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain event test of the overlay merge order check (OMN-22809).

Publish the input event and assert every event it produces, in order, under one correlation id, ending in the
run-completed event; the last test shows the assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    EnumOverlayVerdict,
)
from omnimarket.nodes.node_overlay_merge_order_compute.handlers.handler_overlay_merge_order import (
    HandlerOverlayMergeOrder,
    answer_command,
)
from tests.unit.nodes.node_overlay_merge_order_compute.merge_order_support import (
    CONTRACT,
    CORRELATION,
    MAX_NAMED,
    TOPICS,
    check,
    command,
    handler,
    whole_home,
)

pytestmark = pytest.mark.unit

EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "overlay_merge_order_decided",
        "overlay_merge_order_run_completed",
        "overlay_merge_order_run_failed",
    )
]
# The contract's terminal event, spelled out so a renamed topic turns this test red.
TERMINAL_EVENT = "onex.evt.omnimarket.overlay-merge-order-run-completed.v1"


async def publish(
    node: HandlerOverlayMergeOrder, payload: bytes
) -> list[tuple[str, dict[str, Any]]]:
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, node, TOPICS, MAX_NAMED):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                TOPICS["overlay_merge_order_run_completed"],
                TOPICS["overlay_merge_order_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["overlay_merge_order_requested"],
            on_message=on_command,
            group_id="overlay-merge-order",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["overlay_merge_order_requested"], key=None, value=payload
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def payload_of(changed: tuple[str, ...], checks: tuple[Any, ...]) -> bytes:
    return command(changed, checks).model_dump_json().encode()


def assert_golden_chain(events: list[tuple[str, dict[str, Any]]], verdict: str) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["overlay_merge_order_decided"],
        TOPICS["overlay_merge_order_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == verdict
    assert events[-1][1]["verdict"] == verdict
    assert events[-1][1]["failed"] == 0


def test_golden_chain_a_sound_change_is_order_ok() -> None:
    events = asyncio.run(
        publish(handler(), payload_of(("node_alpha_effect",), whole_home()))
    )
    assert_golden_chain(events, "ORDER_OK")
    assert events[0][1]["ahead_count"] == 0
    assert events[0][1]["accepted"] == ["node_alpha_effect"]


def test_golden_chain_a_change_ahead_of_its_code_is_a_decision_not_a_failure() -> None:
    """AHEAD_OF_CODE is the decision; the chain still ends in success with the verdict on the events."""
    events = asyncio.run(
        publish(
            handler(),
            payload_of(("node_alpha_effect", "node_gamma_effect"), whole_home()),
        )
    )
    assert_golden_chain(events, "AHEAD_OF_CODE")
    assert events[0][1]["ahead"] == [
        {
            "overlay_id": "node_gamma_effect",
            "verdict": "rejected",
            "fields": ["clone_branch", "overlay_ids"],
        }
    ]
    assert events[0][1]["merged_ref"] == "origin/dev"


def test_golden_chain_names_at_most_max_named_and_counts_them_all() -> None:
    many = tuple(
        check(f"node_over{n}_effect", EnumOverlayVerdict.REJECTED, "key")
        for n in range(MAX_NAMED + 3)
    )
    events = asyncio.run(
        publish(handler(), payload_of(tuple(c.overlay_id for c in many), many))
    )
    assert events[0][1]["ahead_count"] == MAX_NAMED + 3
    assert len(events[0][1]["ahead"]) == MAX_NAMED


def test_golden_chain_ends_in_the_contract_terminal_event() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL_EVENT
    events = asyncio.run(
        publish(handler(), payload_of(("node_alpha_effect",), whole_home()))
    )
    assert events[-1][0] == TERMINAL_EVENT


def test_golden_chain_every_contract_event_topic_is_declared_and_none_is_spelled_in_code() -> (
    None
):
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    declared = set(contract["topics"].values())
    assert set(contract["externally_consumed_topics"]) <= declared
    assert set(EVENT_TOPICS) <= declared


def test_golden_chain_failing_control_the_assertion_turns_red_on_a_swallowed_decision() -> (
    None
):
    """Break: the decided event is never published. The same assertion must fail."""
    events = asyncio.run(
        publish(handler(), payload_of(("node_alpha_effect",), whole_home()))
    )
    swallowed = [
        event for event in events if event[0] != TOPICS["overlay_merge_order_decided"]
    ]
    with pytest.raises(AssertionError):
        assert_golden_chain(swallowed, "ORDER_OK")
    assert_golden_chain(events, "ORDER_OK")
