# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Smoke test: kafka_integration_bus fixture publishes and consumes a real event.

OMN-8726 hard gate: this test must pass against the docker-compose.e2e.yml stack
before any Phase 2 Class A integration test may merge. The fixture provisions a
unique consumer topic before subscription and removes only that topic afterward.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import pytest
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

from tests.helpers.kafka_topic_lifecycle import KafkaSmokeTopic

logger = logging.getLogger(__name__)


async def _wait_for_consumer_start(
    kafka_integration_bus: EventBusKafka,
    consuming_task: asyncio.Task[None],
    *,
    timeout_seconds: float,
) -> None:
    """Wait for consumer registration while preserving startup failures."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        if consuming_task.done():
            consuming_task.result()
            raise RuntimeError("Kafka consuming task exited before registering")

        health = await kafka_integration_bus.health_check()
        consumer_count = health.get("consumer_count")
        if isinstance(consumer_count, int) and consumer_count > 0:
            return

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Kafka consumer did not register before publishing")
        await asyncio.sleep(min(0.05, remaining))


@pytest.mark.integration
@pytest.mark.kafka
async def test_kafka_integration_bus_publishes_and_consumes(
    kafka_integration_bus: EventBusKafka,
    kafka_smoke_topic: KafkaSmokeTopic,
) -> None:
    """Publish an event to Kafka and confirm the subscriber receives it."""
    topic = kafka_smoke_topic.topic
    received: list[bytes] = []
    ready = asyncio.Event()

    async def on_message(msg: object) -> None:
        received.append(getattr(msg, "value", b""))
        ready.set()

    unsubscribe = await kafka_integration_bus.subscribe(
        topic=topic,
        group_id=kafka_smoke_topic.group_id,
        on_message=on_message,
    )

    consuming_task = asyncio.create_task(kafka_integration_bus.start_consuming())

    try:
        await _wait_for_consumer_start(
            kafka_integration_bus,
            consuming_task,
            timeout_seconds=15.0,
        )
        payload = json.dumps({"status": "kafka-smoke"}).encode()
        await kafka_integration_bus.publish(topic=topic, key=None, value=payload)
        await asyncio.wait_for(ready.wait(), timeout=15.0)
        logger.info(
            "kafka_smoke_message_consumed topic=%s count=%d",
            topic,
            len(received),
        )
    finally:
        await unsubscribe()
        consuming_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consuming_task

    assert len(received) >= 1, "Expected at least one message from Kafka"
    assert any(b"kafka-smoke" in msg for msg in received)
