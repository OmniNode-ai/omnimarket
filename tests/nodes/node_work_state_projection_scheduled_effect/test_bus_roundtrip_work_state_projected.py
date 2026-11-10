# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime tick goes in and the projected event comes out over a bus: in memory, and on a Redpanda broker (OMN-22935).

Seam: the tick payload another node's scheduler publishes (omnibase_infra's ModelRuntimeTick) against this node's handler,
and the event this node publishes against its output model. A tick that does not validate comes back as a typed error naming
the exception class (the error chain). The in-memory leg runs everywhere; the Redpanda leg runs against the plaintext lab
broker named in ``WORK_STATE_PARITY_BOOTSTRAP`` (one the caller started for the proof, never the dev lane's):

    WORK_STATE_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "work_state_parity and kafka" -q
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import timedelta
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import ValidationError

from omnimarket.models.work_state_projection import ModelWorkStateProjectedEvent
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    T0,
    schedule,
    tick,
)

pytestmark = [pytest.mark.unit, pytest.mark.work_state_parity]

BOOTSTRAP_ENV = "WORK_STATE_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0


def serve_tick(
    handler: HandlerWorkStateProjectionScheduled, raw: bytes
) -> dict[str, Any]:
    """The node's consumer: rebuild the tick from the message, run the handler, answer with the event or the error class."""
    try:
        request = ModelRuntimeTick.model_validate_json(raw)
    except ValidationError as exc:
        return {"error": type(exc).__name__, "count": exc.error_count()}
    event = handler.handle(request)
    return {"event": None if event is None else json.loads(event.model_dump_json())}


async def over_inmemory(messages: list[bytes]) -> list[dict[str, Any]]:
    handler = HandlerWorkStateProjectionScheduled(schedule=schedule())
    bus = EventBusInmemory(environment="work-state-scheduled", group="scheduled")
    prefix = uuid.uuid4().hex
    replies: list[dict[str, Any]] = []
    got = asyncio.Event()
    await bus.start()

    async def serve(message: Any) -> None:
        await bus.publish(
            f"{prefix}.evt",
            None,
            json.dumps(serve_tick(handler, message.value)).encode(),
        )

    async def collect(message: Any) -> None:
        replies.append(json.loads(message.value))
        got.set()

    await bus.subscribe(f"{prefix}.tick", on_message=serve, group_id="serve")
    await bus.subscribe(f"{prefix}.evt", on_message=collect, group_id="collect")
    try:
        for raw in messages:
            got.clear()
            await bus.publish(f"{prefix}.tick", None, raw)
            await asyncio.wait_for(got.wait(), TIMEOUT_S)
    finally:
        await bus.close()
    return replies


async def over_redpanda(messages: list[bytes]) -> list[dict[str, Any]]:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    bootstrap = os.environ[BOOTSTRAP_ENV]
    handler = HandlerWorkStateProjectionScheduled(schedule=schedule())
    prefix = f"work-state-scheduled-{uuid.uuid4().hex}"
    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap, security_protocol="PLAINTEXT"
    )
    await producer.start()
    replies: list[dict[str, Any]] = []
    got = asyncio.Event()
    consumers: list[Any] = []

    async def consume(topic: str, handle: Any) -> None:
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=bootstrap,
            security_protocol="PLAINTEXT",
            group_id=f"{prefix}-{topic}",
            auto_offset_reset="earliest",
        )
        await consumer.start()
        consumers.append(consumer)
        async for message in consumer:
            await handle(message.value)

    async def serve(raw: bytes) -> None:
        await producer.send_and_wait(
            f"{prefix}.evt", json.dumps(serve_tick(handler, raw)).encode()
        )

    async def collect(raw: bytes) -> None:
        replies.append(json.loads(raw))
        got.set()

    tasks = [
        asyncio.create_task(consume(f"{prefix}.tick", serve)),
        asyncio.create_task(consume(f"{prefix}.evt", collect)),
    ]
    try:
        await asyncio.sleep(3.0)  # let the consumers join their topics
        for raw in messages:
            got.clear()
            await producer.send_and_wait(f"{prefix}.tick", raw)
            await asyncio.wait_for(got.wait(), TIMEOUT_S)
    finally:
        for task in tasks:
            task.cancel()
        for consumer in consumers:
            await consumer.stop()
        await producer.stop()
    return replies


def scenario() -> list[bytes]:
    due = tick(T0).model_dump_json().encode()
    inside = tick(T0 + timedelta(seconds=30)).model_dump_json().encode()
    later = tick(T0 + timedelta(seconds=60)).model_dump_json().encode()
    broken = json.dumps({"now": "not a time"}).encode()
    return [due, inside, later, broken]


def check(replies: list[dict[str, Any]]) -> None:
    due, inside, later, broken = replies
    event = ModelWorkStateProjectedEvent.model_validate(due["event"])
    assert event.computed_at == T0
    assert event.item_count == 0
    assert (
        len(event.unread) > 0
    )  # in-memory profile: every source drawn unread, never empty
    assert inside == {
        "event": None
    }  # golden: the schedule gate holds a tick inside the interval
    assert ModelWorkStateProjectedEvent.model_validate(
        later["event"]
    ).computed_at == T0 + timedelta(seconds=60)
    assert (
        broken["error"] == "ValidationError"
    )  # error chain: a tick that does not validate
    assert broken["count"] > 0


def test_in_memory_bus_carries_the_tick_in_and_the_event_out() -> None:
    check(asyncio.run(over_inmemory(scenario())))


@pytest.mark.kafka
@pytest.mark.integration
def test_redpanda_overlay_carries_the_same_events_as_the_in_memory_overlay() -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    check(asyncio.run(over_redpanda(scenario())))
