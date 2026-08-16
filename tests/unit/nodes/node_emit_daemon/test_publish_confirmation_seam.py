# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Cross-boundary seam test: omnimarket's outbox against the real infra bus.

OMN-15861. The seams doctrine requires that two pieces of work which interact
be covered by ONE regression test driving the ACTUAL seam -- not two
individually-green unit suites either side of the boundary, which is the shape
that produced a silent 100% runtime no-op elsewhere.

The seam here is ``EventBus.publish``'s return value crossing from
``omnibase_infra`` into ``omnimarket``'s durable outbox. Both halves are real in
this file:

* ``EventBusInmemory`` -- the real infra bus, started, assigning real offsets.
* ``build_kafka_publish_fn`` -- the real omnimarket adapter. This is the exact
  function that would have swallowed the return value before this change.
* ``ModelPublishReceipt`` -- **never constructed by hand anywhere in this
  file.** Every receipt asserted on came out of a real ``publish()`` call. A
  hand-built receipt would prove only that the test author can spell the model.
* ``InmemoryReadbackSource`` + ``BrokerReadbackStrategy`` -- the real infra
  readback and verdict logic, reading the coordinate back out of the bus's own
  history *independently of what the receipt claimed*.
* ``KafkaPublisherLoop``, ``BoundedEventQueue``, ``DurableOutbox`` -- the real
  omnimarket publisher path, including real truncate-on-ack.

The falsifiable case is ``test_publish_succeeds_but_confirmation_fails_keeps_the_record``:
under the old ack-on-publish-return behaviour the outbox would have truncated a
record no authoritative surface ever vouched for. Zero infrastructure, zero LAN.
"""

from __future__ import annotations

import asyncio

import pytest
from omnibase_infra.enums import EnumInfraTransportType
from omnibase_infra.event_bus.confirmation import (
    BrokerReadbackStrategy,
    InmemoryReadbackSource,
)
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_emit_daemon.confirmation_binding import (
    build_confirmation_bindings,
)
from omnimarket.nodes.node_emit_daemon.event_queue import (
    BoundedEventQueue,
    ModelQueuedEvent,
)
from omnimarket.nodes.node_emit_daemon.kafka_publish import build_kafka_publish_fn
from omnimarket.nodes.node_emit_daemon.models.model_durability import (
    EnumDurabilityTier,
)
from omnimarket.nodes.node_emit_daemon.publisher_loop import KafkaPublisherLoop

SEAM_ENVIRONMENT = "marketseam"
SEAM_CLUSTER = f"inmemory.{SEAM_ENVIRONMENT}"
SEAM_TOPIC = "onex.cmd.omnibase-infra.delegation-request.v1"


def _duty_critical_event(event_id: str) -> ModelQueuedEvent:
    from datetime import UTC, datetime

    return ModelQueuedEvent(
        event_id=event_id,
        event_type="delegation.request",
        topic=SEAM_TOPIC,
        payload={"request_id": event_id},
        queued_at=datetime.now(UTC),
        tier=EnumDurabilityTier.DUTY_CRITICAL,
    )


@pytest.fixture
async def bus():
    """A started, real in-memory infra bus."""
    instance = EventBusInmemory(environment=SEAM_ENVIRONMENT, group="marketseam")
    await instance.start()
    try:
        yield instance
    finally:
        await instance.close()


def _loop_over(
    bus: EventBusInmemory,
    queue: BoundedEventQueue,
    *,
    readback_cluster: str = SEAM_CLUSTER,
) -> KafkaPublisherLoop:
    """Wire the real omnimarket loop onto the real infra bus.

    ``readback_cluster`` defaults to the bus's own identity. Passing a different
    one produces a readback source that genuinely cannot vouch for this bus's
    coordinates -- the "produce accepted, durability unproven" case, expressed
    through the real cluster-identity check rather than a mock.
    """
    return KafkaPublisherLoop(
        queue=queue,
        publish_fn=build_kafka_publish_fn(bus, source="omnimarket-seam"),
        backoff_base_seconds=0.01,
        max_backoff_seconds=0.02,
        confirmation_bindings=build_confirmation_bindings(
            duty_critical=BrokerReadbackStrategy(
                InmemoryReadbackSource(bus, cluster=readback_cluster),
                readback_deadline_seconds=0.5,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_publish_fn_returns_the_coordinate_the_bus_actually_assigned(
    bus: EventBusInmemory,
) -> None:
    """The adapter propagates a real receipt, field by field.

    The offset is checked against the bus's own ``get_topic_offset`` rather than
    a constant, so a receipt reporting a position the bus never assigned fails
    here instead of silently authorising a truncation later.
    """
    publish_fn = build_kafka_publish_fn(bus, source="omnimarket-seam")

    receipt = await publish_fn(
        SEAM_TOPIC,
        None,
        b'{"request_id": "r1"}',
        {"idempotency_key": "evt-1", "event_type": "delegation.request"},
    )

    assert receipt is not None, "the adapter must not swallow the return value"
    assert receipt.topic == SEAM_TOPIC
    assert receipt.partition == 0
    assert receipt.offset == 0, "0 is a valid coordinate, not a falsy sentinel"
    assert receipt.cluster == SEAM_CLUSTER
    assert receipt.transport is EnumInfraTransportType.INMEMORY
    assert receipt.idempotency_key == "evt-1", (
        "the logical-event identity must survive the omnimarket->infra hop"
    )
    # The bus's own counter is now past the offset it handed back.
    assert await bus.get_topic_offset(SEAM_TOPIC) == 1


@pytest.mark.asyncio
async def test_confirmed_publish_truncates_the_outbox_record(
    bus: EventBusInmemory, tmp_path
) -> None:
    """Happy path across the boundary: publish -> independent readback -> ack."""
    queue = BoundedEventQueue(
        spool_dir=tmp_path / "spool", outbox_dir=tmp_path / "outbox"
    )
    loop = _loop_over(bus, queue)

    await queue.enqueue(_duty_critical_event("c1"))
    assert queue.outbox_pending() == 1

    await loop.start()
    for _ in range(100):
        if queue.outbox_pending() == 0:
            break
        await asyncio.sleep(0.02)
    await loop.stop()

    assert queue.outbox_pending() == 0, "a confirmed record must be truncated"
    assert loop.events_published == 1
    assert loop.events_unconfirmed == 0

    # Independent evidence: the record really is on the bus's history, and it
    # carries the idempotency key the outbox event was identified by.
    history = await bus.get_event_history(topic=SEAM_TOPIC)
    assert len(history) == 1
    assert history[0].headers.idempotency_key == "c1"


@pytest.mark.asyncio
async def test_publish_succeeds_but_confirmation_fails_keeps_the_record(
    bus: EventBusInmemory, tmp_path
) -> None:
    """Zero false durable claims -- the assertion the old shape could not make.

    The publish genuinely succeeds: the bus assigns a coordinate and the record
    lands in its history. Only the *confirmation* fails, because the readback
    source is bound to a different cluster identity and so cannot vouch for this
    bus's coordinates.

    Under ack-on-publish-return the outbox would have drained to zero here, on
    the strength of a call that returned. It must stay pending.
    """
    queue = BoundedEventQueue(
        spool_dir=tmp_path / "spool", outbox_dir=tmp_path / "outbox"
    )
    loop = _loop_over(bus, queue, readback_cluster="inmemory.some-other-cluster")

    await queue.enqueue(_duty_critical_event("u1"))
    assert queue.outbox_pending() == 1

    await loop.start()
    await asyncio.sleep(0.5)
    await loop.stop()

    # The produce really happened -- this is not a publish failure.
    history = await bus.get_event_history(topic=SEAM_TOPIC)
    assert history, "the publish itself must have succeeded"

    assert queue.outbox_pending() == 1, (
        "an unconfirmed record must NOT be truncated -- this is the "
        "false-durable-claim the ticket removes"
    )
    assert loop.events_published == 0, "no durable claim may be made"
    assert loop.events_unconfirmed > 0
    assert loop.events_dropped == 0
