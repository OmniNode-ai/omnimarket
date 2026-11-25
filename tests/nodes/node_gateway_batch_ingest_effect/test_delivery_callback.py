# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC16: per-record confirmation from each record's own callback, never a producer-wide flush."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from omnimarket.nodes.node_gateway_batch_ingest_effect.kafka_publisher import (
    KafkaConfirmingPublisher,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelWireRecord,
)


class _Producer:
    """Fires scripted callbacks on poll; a topic with no script never calls back."""

    def __init__(
        self, script: dict[str, object], *, raises: Exception | None = None
    ) -> None:
        self.script = script
        self.raises = raises
        self.pending: list[tuple[str, Callable[[object, object], None]]] = []
        self.flushes = 0

    def produce(
        self,
        topic: str,
        *,
        value: bytes,
        key: bytes | None,
        headers: list[tuple[str, bytes]],
        on_delivery: Callable[[object, object], None],
    ) -> None:
        if self.raises is not None:
            raise self.raises
        self.pending.append((topic, on_delivery))

    def poll(self, timeout: float) -> int:
        fired = [p for p in self.pending if p[0] in self.script]
        self.pending = [p for p in self.pending if p[0] not in self.script]
        for topic, callback in fired:
            callback(self.script[topic], None)
        return len(fired)

    def flush(self, timeout: float) -> int:
        self.flushes += 1
        return 0


def _record(topic: str) -> ModelWireRecord:
    return ModelWireRecord(
        topic=topic,
        key=None,
        value=b"v",
        headers=(),
        event_id="0" * 64,
        kafka_record_size=1,
    )


def _publisher(producer: _Producer, timeout: float = 0.2) -> KafkaConfirmingPublisher:
    return KafkaConfirmingPublisher(
        producer, message_max_bytes=4096, confirm_timeout_s=timeout
    )


async def test_delivery_callback_two_overlapping_sends_report_through_their_own_callback() -> (
    None
):
    producer = _Producer({"x": "KafkaError(MSG_TIMED_OUT)", "y": None})
    publisher = _publisher(producer)
    x, y = await asyncio.gather(
        publisher.publish(_record("x")), publisher.publish(_record("y"))
    )
    assert (x.status, y.status) == ("publish-refused", "published")
    assert producer.flushes == 0


async def test_delivery_callback_reversed_outcomes_are_not_moved_by_the_other_send() -> (
    None
):
    producer = _Producer({"x": None, "y": "KafkaError(MSG_SIZE_TOO_LARGE)"})
    publisher = _publisher(producer)
    x, y = await asyncio.gather(
        publisher.publish(_record("x")), publisher.publish(_record("y"))
    )
    assert (x.status, y.status) == ("published", "publish-refused")


async def test_delivery_callback_a_callback_that_never_fires_is_delivery_unconfirmed() -> (
    None
):
    publisher = _publisher(_Producer({}), timeout=0.05)
    outcome = await publisher.publish(_record("silent"))
    assert outcome.status == "delivery-unconfirmed"


async def test_delivery_callback_a_synchronous_produce_failure_is_publish_refused() -> (
    None
):
    publisher = _publisher(_Producer({}, raises=BufferError("queue full")))
    outcome = await publisher.publish(_record("x"))
    assert (outcome.status, outcome.detail) == ("publish-refused", "BufferError")
