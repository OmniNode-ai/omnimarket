# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Kafka-down chaos + restart proof for tiered durability (OMN-12620).

Proves the recorded RPO policy through the real publisher loop:

  * Kafka down -> duty-critical events survive in the durable outbox and are
    replayed once Kafka recovers (zero loss). Telemetry events may drop per
    policy.
  * A daemon restart re-loads pending outbox events so replay resumes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_emit_daemon.event_queue import (
    BoundedEventQueue,
    ModelQueuedEvent,
)
from omnimarket.nodes.node_emit_daemon.models.model_durability import (
    EnumDurabilityTier,
)
from omnimarket.nodes.node_emit_daemon.publisher_loop import KafkaPublisherLoop

pytestmark = pytest.mark.unit


def _event(event_id: str, tier: EnumDurabilityTier) -> ModelQueuedEvent:
    return ModelQueuedEvent(
        event_id=event_id,
        event_type="delegation.request"
        if tier is EnumDurabilityTier.DUTY_CRITICAL
        else "latency.breakdown",
        topic="onex.cmd.omnibase-infra.delegation-request.v1"
        if tier is EnumDurabilityTier.DUTY_CRITICAL
        else "onex.evt.omniclaude.latency-breakdown.v1",
        payload={"correlation_id": event_id},
        partition_key=event_id,
        queued_at=datetime.now(UTC),
        tier=tier,
    )


class _FlakyKafka:
    """Publish sink that is down until ``up`` is set, then records publishes."""

    def __init__(self) -> None:
        self.up = False
        self.published: list[str] = []

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: dict[str, str],
    ) -> None:
        if not self.up:
            raise ConnectionError("kafka down")
        self.published.append(topic)


@pytest.mark.asyncio
async def test_kafka_down_duty_critical_survives_then_replays(tmp_path: Path) -> None:
    outbox_dir = tmp_path / "outbox"
    spool_dir = tmp_path / "spool"
    queue = BoundedEventQueue(
        max_memory_queue=4,
        max_spool_messages=4,
        spool_dir=spool_dir,
        outbox_dir=outbox_dir,
    )
    kafka = _FlakyKafka()  # down
    loop = KafkaPublisherLoop(
        queue=queue,
        publish_fn=kafka.publish,
        failure_threshold=2,
        recovery_timeout=0.2,
        backoff_base_seconds=0.01,
        max_backoff_seconds=0.05,
    )

    # Enqueue duty-critical events while Kafka is down.
    for i in range(3):
        await queue.enqueue(_event(f"d{i}", EnumDurabilityTier.DUTY_CRITICAL))
    assert queue.outbox_pending() == 3

    await loop.start()
    # Kafka is down: events must NOT be published or dropped; they stay durable.
    await asyncio.sleep(0.4)
    assert kafka.published == []
    assert queue.outbox_pending() == 3  # zero loss while Kafka down

    # Kafka recovers -> events replay and the outbox truncates on ack.
    kafka.up = True
    for _ in range(50):
        if queue.outbox_pending() == 0:
            break
        await asyncio.sleep(0.1)
    await loop.stop()

    assert queue.outbox_pending() == 0
    assert (
        sorted(kafka.published) == ["onex.cmd.omnibase-infra.delegation-request.v1"] * 3
    )
    # No duty-critical event was dropped.
    assert loop.events_dropped == 0


@pytest.mark.asyncio
async def test_outbox_pending_survives_simulated_restart(tmp_path: Path) -> None:
    outbox_dir = tmp_path / "outbox"
    spool_dir = tmp_path / "spool"

    # Producer enqueues duty-critical events; publisher never gets to drain.
    q1 = BoundedEventQueue(spool_dir=spool_dir, outbox_dir=outbox_dir)
    await q1.enqueue(_event("d1", EnumDurabilityTier.DUTY_CRITICAL))
    await q1.enqueue(_event("d2", EnumDurabilityTier.DUTY_CRITICAL))

    # Simulate daemon restart: new queue over same dirs, load pending outbox.
    q2 = BoundedEventQueue(spool_dir=spool_dir, outbox_dir=outbox_dir)
    loaded = await q2.load_outbox()
    assert loaded == 2

    kafka = _FlakyKafka()
    kafka.up = True
    loop = KafkaPublisherLoop(queue=q2, publish_fn=kafka.publish)
    await loop.start()
    for _ in range(50):
        if q2.outbox_pending() == 0:
            break
        await asyncio.sleep(0.05)
    await loop.stop()

    assert q2.outbox_pending() == 0
    assert (
        sorted(kafka.published) == ["onex.cmd.omnibase-infra.delegation-request.v1"] * 2
    )
