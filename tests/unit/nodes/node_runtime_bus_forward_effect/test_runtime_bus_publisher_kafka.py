# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hosted-bus publisher of node_runtime_bus_forward_effect, on a recorded producer (OMN-24003).

The producer is the two calls of a confluent-kafka producer the publisher makes; the real broker reading is the lab proof.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from omnimarket.nodes.node_runtime_bus_forward_effect.protocols.protocol_runtime_bus_publisher import (
    PublisherRefusedError,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.publishers.publisher_kafka import (
    KafkaRuntimeBusPublisher,
)

pytestmark = pytest.mark.unit


class RecordedProducer:
    def __init__(self, *, remaining: int = 0, fail_delivery: str | None = None) -> None:
        self.produced: list[dict[str, object]] = []
        self.remaining = remaining
        self.fail_delivery = fail_delivery
        self.raise_on_produce: Exception | None = None

    def produce(
        self,
        topic: str,
        *,
        value: bytes,
        key: bytes | None,
        headers: list[tuple[str, bytes]],
        on_delivery: Callable[[object, object], None],
    ) -> None:
        if self.raise_on_produce is not None:
            raise self.raise_on_produce
        self.produced.append(
            {"topic": topic, "value": value, "key": key, "headers": headers}
        )
        on_delivery(self.fail_delivery, None)

    def flush(self, timeout: float) -> int:
        return self.remaining


class TestGoldenChain:
    def test_publish_hands_topic_key_value_and_encoded_headers_to_the_producer(
        self,
    ) -> None:
        producer = RecordedProducer()
        publisher = KafkaRuntimeBusPublisher(producer)

        publisher.publish("example.evt.x.v1", b"k", b"v", {"correlation_id": "c-1"})

        assert producer.produced == [
            {
                "topic": "example.evt.x.v1",
                "value": b"v",
                "key": b"k",
                "headers": [("correlation_id", b"c-1")],
            }
        ]
        assert publisher.flush(5.0) == 0


class TestErrorChain:
    def test_a_producer_that_refuses_the_publish_raises_the_nodes_refusal(self) -> None:
        producer = RecordedProducer()
        producer.raise_on_produce = BufferError("queue full")
        publisher = KafkaRuntimeBusPublisher(producer)

        with pytest.raises(PublisherRefusedError, match="queue full"):
            publisher.publish("example.evt.x.v1", None, b"v", {})

    def test_a_delivery_error_and_a_remaining_message_both_count_as_undelivered(
        self,
    ) -> None:
        producer = RecordedProducer(
            remaining=1, fail_delivery="TOPIC_AUTHORIZATION_FAILED"
        )
        publisher = KafkaRuntimeBusPublisher(producer)

        publisher.publish("example.evt.x.v1", None, b"v", {})

        assert publisher.flush(5.0) == 2
        assert publisher.failure_detail() == "TOPIC_AUTHORIZATION_FAILED"
