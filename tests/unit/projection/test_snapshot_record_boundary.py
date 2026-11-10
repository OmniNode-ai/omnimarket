# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reproduce the serialized sizes reported by the delegation writer's retry loop."""

from __future__ import annotations

import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiokafka import AIOKafkaProducer
from aiokafka.errors import MessageSizeTooLargeError
from aiokafka.record.default_records import DefaultRecordBatchBuilder

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.projection.snapshot_publisher import (
    ModelSnapshotDeltaMessage,
    SnapshotPayloadTooLargeError,
    assert_snapshot_within_bound,
    resolve_snapshot_max_payload_bytes,
)
from tests.test_projection_error_classification_omn13634 import (
    _ClassifyingRunner,
    _CommitRecordingConsumer,
    _wrapped_msg,
)

# Recorded 2026-11-04 12:56:00 and 12:56:38 UTC: the running delegation writer
# retried these serialized sizes ten times, then its container restarted.
_RECORDED_SERIALIZED_SIZES = (1_048_609, 1_048_606)
_LIMIT = 1_048_576
_EXPOSURE = ProjectionTableConfig(
    topic="onex.snapshot.projection.boundary-test.v1",
    table="boundary_test",
    columns=("id", "value"),
    bus_backed=True,
    key_columns=("id",),
)


def _message(serialized_size: int) -> ModelSnapshotDeltaMessage:
    key = b"row"
    headers = (("tenant_id", b"tenant"),)
    value_size = serialized_size
    for _ in range(4):
        measured = DefaultRecordBatchBuilder.estimate_size_in_bytes(
            key, b"x" * value_size, list(headers)
        )
        value_size += serialized_size - measured
    message = ModelSnapshotDeltaMessage(
        topic=_EXPOSURE.topic,
        key=key,
        value=b"x" * value_size,
        headers=headers,
    )
    assert (
        DefaultRecordBatchBuilder.estimate_size_in_bytes(
            message.key, message.value, list(message.headers)
        )
        == serialized_size
    )
    return message


class _Runner(BaseProjectionRunner):
    @property
    def topics(self) -> list[str]:
        return []

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        return True


def test_default_bound_matches_the_runner_owned_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KAFKA_MAX_REQUEST_SIZE", raising=False)
    default = inspect.signature(AIOKafkaProducer).parameters["max_request_size"].default
    assert default == _LIMIT
    assert resolve_snapshot_max_payload_bytes() == default


@pytest.mark.asyncio
@pytest.mark.parametrize("serialized_size", _RECORDED_SERIALIZED_SIZES)
async def test_recorded_retry_is_refused_before_the_transport(
    serialized_size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KAFKA_MAX_REQUEST_SIZE", raising=False)
    message = _message(serialized_size)
    assert message.value is not None
    assert len(message.value) < _LIMIT
    producer = AIOKafkaProducer(bootstrap_servers="unused.test:9092")
    try:
        # Positive control for the original failure: use the installed driver's
        # serializer without opening a connection or publishing a record.
        with pytest.raises(MessageSizeTooLargeError):
            producer._serialize(message.key, message.value, list(message.headers))
        runner = _Runner()
        with (
            patch(
                "omnimarket.projection.runner.encode_snapshot_delta",
                return_value=message,
            ),
            patch.object(runner, "_ensure_producer", new_callable=AsyncMock) as ensure,
            pytest.raises(SnapshotPayloadTooLargeError) as caught,
        ):
            await runner.publish_snapshot_delta(
                _EXPOSURE,
                op="upsert",
                row={"id": "row", "value": "recorded boundary"},
                source_event_id="boundary-source",
                source_topic="onex.evt.omnimarket.boundary-test.v1",
                source_partition=0,
                source_offset=41,
            )
        assert caught.value.size_bytes == serialized_size
        assert caught.value.limit_bytes == _LIMIT
        ensure.assert_not_awaited()
        # Keep the refusal visible and durable through the existing runner
        # policy; the source advances only after the DLQ publish succeeds.
        quarantine = _ClassifyingRunner(raises=caught.value)
        consumer = _CommitRecordingConsumer()
        quarantine._consumer = consumer
        await quarantine._handle_message(
            _wrapped_msg(quarantine.topics[0], 41, {"correlation_id": "boundary"})
        )
        assert quarantine._stats.errors_count == 1
        assert [list(commit.values()) for commit in consumer.commits] == [[42]]
        ((topic, value),) = quarantine.dlq_published
        assert topic == quarantine.poison_dlq_topics[0]
        envelope = json.loads(value)
        assert envelope["correlation_id"] == "boundary"
        assert f"{serialized_size:,}" in envelope["failure_reason"]
        failed_quarantine = _ClassifyingRunner(raises=caught.value)
        failed_consumer = _CommitRecordingConsumer()
        failed_quarantine._consumer = failed_consumer
        with (
            patch.object(
                failed_quarantine, "publish_dlq", side_effect=ConnectionError("offline")
            ),
            pytest.raises(SnapshotPayloadTooLargeError),
        ):
            await failed_quarantine._handle_message(
                _wrapped_msg(
                    failed_quarantine.topics[0], 41, {"correlation_id": "boundary"}
                )
            )
        assert failed_consumer.commits == []
    finally:
        await producer.stop()


@pytest.mark.asyncio
async def test_exact_serialized_limit_is_accepted_by_transport() -> None:
    message = _message(_LIMIT)
    producer = AIOKafkaProducer(bootstrap_servers="unused.test:9092")
    try:
        assert_snapshot_within_bound(message, limit_bytes=_LIMIT)
        producer._serialize(message.key, message.value, list(message.headers))
    finally:
        await producer.stop()


def test_tombstone_key_and_headers_are_bounded() -> None:
    message = ModelSnapshotDeltaMessage(
        topic=_EXPOSURE.topic, key=b"x" * _LIMIT, value=None, headers=()
    )
    with pytest.raises(SnapshotPayloadTooLargeError):
        assert_snapshot_within_bound(message, limit_bytes=_LIMIT)


def test_header_bytes_count_toward_the_serialized_bound() -> None:
    message = ModelSnapshotDeltaMessage(
        topic=_EXPOSURE.topic,
        key=b"row",
        value=b"x",
        headers=(("large", b"x" * _LIMIT),),
    )
    with pytest.raises(SnapshotPayloadTooLargeError):
        assert_snapshot_within_bound(message, limit_bytes=_LIMIT)
