# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Smoke test: kafka_integration_bus fixture publishes and consumes a real event.

OMN-8726 hard gate: this test must pass against the docker-compose.e2e.yml stack
before any Phase 2 Class A integration test may merge.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka


@pytest.mark.integration
@pytest.mark.kafka
async def test_kafka_integration_bus_publishes_and_consumes(
    kafka_integration_bus: EventBusKafka,
) -> None:
    """Publish an event to Kafka and confirm the subscriber receives it."""
    topic = "onex.evt.omnimarket.integration-kafka-smoke.v1"
    received: list[bytes] = []
    ready = asyncio.Event()

    async def on_message(msg: object) -> None:
        received.append(getattr(msg, "value", b""))
        ready.set()

    unsubscribe = await kafka_integration_bus.subscribe(
        topic=topic,
        group_id="omnimarket-integration-smoke",
        on_message=on_message,
    )

    await kafka_integration_bus.start_consuming()

    payload = json.dumps({"status": "kafka-smoke"}).encode()
    await kafka_integration_bus.publish(topic=topic, key=None, value=payload)

    try:
        await asyncio.wait_for(ready.wait(), timeout=15.0)
    finally:
        await unsubscribe()

    assert len(received) >= 1, "Expected at least one message from Kafka"
    assert any(b"kafka-smoke" in msg for msg in received)
