# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Kafka-down chaos + restart proof for tiered durability (OMN-12620).

Proves the recorded RPO policy through the real publisher loop:

  * Kafka down -> duty-critical events survive in the durable outbox and are
    replayed once Kafka recovers (zero loss). Telemetry events may drop per
    policy.
  * A daemon restart re-loads pending outbox events so replay resumes.

INVERTED under OMN-15861. These tests used to pass with a sink whose ``publish``
returned ``None``, because the loop acked on the publish call not raising. That
made them silently unable to distinguish "the record is durable" from "the
publish call completed" -- the exact confusion the ticket removes. ``_FlakyKafka``
now returns a real ``ModelPublishReceipt`` and records it in a readback surface,
and every loop under test is wired with a real ``BrokerReadbackStrategy``. A
regression to ack-on-return would now fail
``test_publish_without_confirmation_never_acks``, which is the assertion the old
shape could not express.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_infra.enums import EnumInfraTransportType
from omnibase_infra.event_bus.confirmation import BrokerReadbackStrategy
from omnibase_infra.event_bus.models import ModelPublishReceipt

from omnimarket.nodes.node_emit_daemon.confirmation_binding import (
    build_confirmation_bindings,
)
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
    """Publish sink that is down until ``up`` is set, then records publishes.

    Doubles as its own readback surface: every accepted publish is stored under
    its assigned coordinate, and ``observe`` answers from that store. This is
    what makes the confirmation in these tests a real round trip rather than a
    stub that always says yes.
    """

    def __init__(self) -> None:
        self.up = False
        self.published: list[str] = []
        self._offsets: dict[str, int] = {}
        self._landed: set[tuple[str, int, int]] = set()
        # When True, publishes are accepted but the readback surface denies
        # them -- the "produce succeeded, durability unproven" case.
        self.readback_blind = False

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: dict[str, str],
    ) -> ModelPublishReceipt:
        if not self.up:
            raise ConnectionError("kafka down")
        offset = self._offsets.get(topic, 0)
        self._offsets[topic] = offset + 1
        self.published.append(topic)
        if not self.readback_blind:
            self._landed.add((topic, 0, offset))
        return ModelPublishReceipt(
            topic=topic,
            partition=0,
            offset=offset,
            cluster=_TEST_CLUSTER,
            produced_at=datetime.now(UTC),
            transport=EnumInfraTransportType.KAFKA,
            idempotency_key=headers.get("idempotency_key"),
        )

    @property
    def transport(self) -> EnumInfraTransportType:
        return EnumInfraTransportType.KAFKA

    async def observe(
        self, receipt: ModelPublishReceipt, *, deadline_seconds: float
    ) -> bool:
        return (receipt.topic, receipt.partition, receipt.offset) in self._landed


_TEST_CLUSTER = "chaos-broker:9092"


def _bindings(kafka: _FlakyKafka) -> dict[EnumDurabilityTier, object]:
    """Real readback confirmation over the sink's own landed-record store."""
    return build_confirmation_bindings(
        duty_critical=BrokerReadbackStrategy(kafka, readback_deadline_seconds=0.5),
    )


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
        confirmation_bindings=_bindings(kafka),
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
    loop = KafkaPublisherLoop(
        queue=q2,
        publish_fn=kafka.publish,
        confirmation_bindings=_bindings(kafka),
    )
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


@pytest.mark.asyncio
async def test_publish_without_confirmation_never_acks(tmp_path: Path) -> None:
    """The assertion the old ack-on-return shape could not express (OMN-15861).

    Kafka is UP and every publish succeeds -- ``published`` grows -- but the
    readback surface never sees the records. Under the old behaviour the outbox
    would have drained to zero on the strength of the publish call returning.
    It must now stay full: zero acks, zero false durable claims, and the
    unconfirmed count rising instead of ``events_published``.
    """
    outbox_dir = tmp_path / "outbox"
    spool_dir = tmp_path / "spool"
    queue = BoundedEventQueue(spool_dir=spool_dir, outbox_dir=outbox_dir)
    kafka = _FlakyKafka()
    kafka.up = True
    kafka.readback_blind = True

    loop = KafkaPublisherLoop(
        queue=queue,
        publish_fn=kafka.publish,
        backoff_base_seconds=0.01,
        max_backoff_seconds=0.02,
        confirmation_bindings=_bindings(kafka),
    )

    await queue.enqueue(_event("u1", EnumDurabilityTier.DUTY_CRITICAL))
    assert queue.outbox_pending() == 1

    await loop.start()
    await asyncio.sleep(0.5)
    await loop.stop()

    assert kafka.published, "the publish itself must have succeeded"
    assert queue.outbox_pending() == 1, "unconfirmed record must NOT be truncated"
    assert loop.events_published == 0, "no durable claim may be made"
    assert loop.events_unconfirmed > 0
    assert loop.events_dropped == 0


@pytest.mark.asyncio
async def test_unbound_confirmation_fails_closed(tmp_path: Path) -> None:
    """A loop with no confirmation strategy publishes but never acks.

    Guards the misconfiguration path: a missing binding must not degrade to
    "trust the publish return", which is how the weak default would creep back.
    """
    queue = BoundedEventQueue(
        spool_dir=tmp_path / "spool", outbox_dir=tmp_path / "outbox"
    )
    kafka = _FlakyKafka()
    kafka.up = True
    loop = KafkaPublisherLoop(
        queue=queue,
        publish_fn=kafka.publish,
        backoff_base_seconds=0.01,
        max_backoff_seconds=0.02,
    )

    await queue.enqueue(_event("n1", EnumDurabilityTier.DUTY_CRITICAL))
    await loop.start()
    await asyncio.sleep(0.4)
    await loop.stop()

    assert queue.outbox_pending() == 1
    assert loop.events_published == 0
