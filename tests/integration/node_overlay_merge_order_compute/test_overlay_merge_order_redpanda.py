# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam run of the overlay merge order node on the real broker (OMN-22809).

The same chain the in-memory golden and error chain tests assert, over the integration lane's broker: the command is
published on the contract's request topic, the node answers on the contract's event topics, and the events arrive in
order under one correlation id. Requires the integration lane's broker (the ``kafka_integration_bus`` fixture).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

from omnimarket.nodes.node_overlay_merge_order_compute.handlers.handler_overlay_merge_order import (
    HandlerOverlayMergeOrder,
    answer_command,
)
from tests.unit.nodes.node_overlay_merge_order_compute.merge_order_support import (
    MAX_NAMED,
    TOPICS,
    command,
    handler,
    whole_home,
)

pytestmark = [pytest.mark.integration, pytest.mark.kafka]

TIMEOUT_SECONDS = 20.0


async def run_chain(
    bus: EventBusKafka, node: HandlerOverlayMergeOrder, payload: bytes
) -> list[tuple[str, dict[str, Any]]]:
    run = uuid.uuid4().hex[:8]
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, node, TOPICS, MAX_NAMED):
            await bus.publish(topic=topic, key=None, value=body)

    def collector(topic: str) -> Any:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                TOPICS["overlay_merge_order_run_completed"],
                TOPICS["overlay_merge_order_run_failed"],
            ):
                done.set()

        return collect

    unsubscribers = [
        await bus.subscribe(
            topic=TOPICS["overlay_merge_order_requested"],
            group_id=f"overlay-merge-order-{run}",
            on_message=on_command,
        )
    ]
    for role in (
        "overlay_merge_order_decided",
        "overlay_merge_order_run_completed",
        "overlay_merge_order_run_failed",
    ):
        unsubscribers.append(
            await bus.subscribe(
                topic=TOPICS[role],
                group_id=f"overlay-merge-order-test-{role}-{run}",
                on_message=collector(TOPICS[role]),
            )
        )
    await bus.start_consuming()
    try:
        await bus.publish(
            topic=TOPICS["overlay_merge_order_requested"], key=None, value=payload
        )
        await asyncio.wait_for(done.wait(), timeout=TIMEOUT_SECONDS)
    finally:
        for unsubscribe in unsubscribers:
            await unsubscribe()
    return seen


async def test_golden_chain_on_the_real_broker_a_change_ahead_of_its_code(
    kafka_integration_bus: EventBusKafka,
) -> None:
    correlation = f"redpanda-golden-{uuid.uuid4().hex[:8]}"
    payload = (
        command(("node_alpha_effect", "node_gamma_effect"), whole_home())
        .model_copy(update={"correlation_id": correlation})
        .model_dump_json()
        .encode()
    )
    events = [
        event
        for event in await run_chain(kafka_integration_bus, handler(), payload)
        if event[1].get("correlation_id") == correlation
    ]
    assert [topic for topic, _ in events] == [
        TOPICS["overlay_merge_order_decided"],
        TOPICS["overlay_merge_order_run_completed"],
    ]
    assert events[0][1]["verdict"] == "AHEAD_OF_CODE"
    assert events[0][1]["ahead"][0]["overlay_id"] == "node_gamma_effect"
    assert events[-1][1]["failed"] == 0


async def test_error_chain_on_the_real_broker_a_touched_overlay_never_judged_is_refused(
    kafka_integration_bus: EventBusKafka,
) -> None:
    correlation = f"redpanda-error-{uuid.uuid4().hex[:8]}"
    payload = (
        command(("node_zeta_effect",), whole_home())
        .model_copy(update={"correlation_id": correlation})
        .model_dump_json()
        .encode()
    )
    events = [
        event
        for event in await run_chain(kafka_integration_bus, handler(), payload)
        if event[1].get("correlation_id") == correlation
    ]
    assert [topic for topic, _ in events] == [TOPICS["overlay_merge_order_run_failed"]]
    assert events[0][1]["failure"] == "changed_overlay_not_judged:node_zeta_effect"
