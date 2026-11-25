# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC10: Kafka size admission from overlay values only; the producer is built with the overlay's message.max.bytes."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_gateway_batch_ingest_effect.kafka_publisher import (
    KafkaConfirmingPublisher,
    producer_config,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    RECORD_OVERHEAD,
    STAMPS_BYTES,
    TOPIC_A,
    TOPIC_B,
    FakePublisher,
    batch,
    make_app,
    make_overlay,
    post,
    record,
)

LIMIT = {TOPIC_A: 4096, TOPIC_B: 2048}


def _value_for(topic: str, extra: int = 0) -> bytes:
    return b"v" * (LIMIT[topic] - RECORD_OVERHEAD - STAMPS_BYTES + extra)


@pytest.mark.parametrize("topic", [TOPIC_A, TOPIC_B])
async def test_kafka_size_admission_at_the_limit_is_published_and_one_over_is_refused(
    topic: str,
) -> None:
    app, publisher = make_app()
    at_limit = await post(app, batch(record(topic, _value_for(topic))))
    assert at_limit.status == 202
    assert publisher.calls[0].kafka_record_size == LIMIT[topic]
    over = await post(app, batch(record(topic, _value_for(topic, 1))))
    assert (over.status, over.body["error"], over.body["index"]) == (
        422,
        "record-exceeds-kafka-limit",
        0,
    )
    assert len(publisher.calls) == 1


async def test_kafka_size_admission_stamps_are_counted() -> None:
    app, _ = make_app(make_overlay(max_egress_value_bytes=5000, max_value_chars=8192))
    without_stamps = b"v" * (
        4096 - RECORD_OVERHEAD
    )  # fits only if the four stamp headers were left out
    result = await post(app, batch(record(TOPIC_A, without_stamps)))
    assert (result.status, result.body["error"]) == (422, "record-exceeds-kafka-limit")


async def test_kafka_size_admission_checks_the_topic_bound_not_only_the_producer_bound() -> (
    None
):
    app, _ = make_app()
    value = b"v" * 3000  # inside the producer's 4096, over topic B's 2048
    assert (await post(app, batch(record(TOPIC_B, value)))).status == 422
    assert (await post(app, batch(record(TOPIC_A, value)))).status == 202


async def test_kafka_size_admission_a_bad_record_in_the_batch_means_call_count_zero() -> (
    None
):
    app, publisher = make_app()
    result = await post(
        app, batch(record(), record(TOPIC_B, _value_for(TOPIC_B, 1)), record())
    )
    assert (result.status, result.body["index"]) == (422, 1)
    assert publisher.calls == []


async def test_kafka_size_admission_an_oversized_record_is_never_a_retryable_503() -> (
    None
):
    app, _ = make_app()
    result = await post(app, batch(record(TOPIC_A, _value_for(TOPIC_A, 1))))
    assert result.status == 422
    assert "retryable" not in result.body


async def test_kafka_size_admission_a_wrong_overlay_is_500_kafka_limit_drift() -> None:
    app, publisher = make_app(publisher=FakePublisher(message_max_bytes=4095))
    result = await post(app, batch(record()))
    assert (result.status, result.body) == (
        500,
        {"error": "kafka-limit-drift", "retryable": False},
    )
    assert publisher.calls == []


def test_kafka_size_admission_producer_is_built_with_the_overlay_message_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    class _Producer:
        def __init__(self, config: dict[str, Any]) -> None:
            captured.append(config)

    monkeypatch.setitem(
        sys.modules, "confluent_kafka", types.SimpleNamespace(Producer=_Producer)
    )
    overlay = make_overlay()
    broker = ModelNodeBroker(
        kind="kafka", bootstrap_servers="broker.example.invalid:9092"
    )
    publisher = KafkaConfirmingPublisher.from_overlay(overlay, {}, broker=broker)
    assert (
        captured[0]["message.max.bytes"]
        == overlay.kafka.producer_max_request_bytes
        == 4096
    )
    assert captured[0]["message.timeout.ms"] == overlay.kafka.message_timeout_ms
    assert publisher.message_max_bytes == 4096
    assert producer_config(broker, overlay)["message.max.bytes"] == 4096


def test_kafka_size_admission_limits_come_from_the_overlay_only() -> None:
    import inspect

    from omnimarket.nodes.node_gateway_batch_ingest_effect import record_admission

    source = inspect.getsource(record_admission)
    assert "AdminClient" not in source
    assert "describe_configs" not in source
    assert "confluent_kafka" not in source
    assert (
        sys.modules.get(
            "omnimarket.nodes.node_gateway_batch_ingest_effect.kafka_publisher"
        )
        is not None
    )
