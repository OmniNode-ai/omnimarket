"""OMN-19448: an oversized poison terminal must not wedge its partition."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from omnimarket.projection import dlq
from tests.test_projection_error_classification_omn13634 import (
    DELEGATION_DLQ_TOPIC,
    _CommitRecordingConsumer,
    _wrapped_msg,
)
from tests.unit.projection.test_delegation_data_source_experiment_omn19448 import (
    recorded_terminal,
    recording_runner,
)


def oversized_poison() -> dict[str, Any]:
    payload = recorded_terminal()
    payload.pop("tenant_id")
    payload["task_type"] = "summarization"
    text = "Eval E-oversized: summarize: " + "lorem ipsum dolor sit amet " * 33000
    payload["prompt_text"] = payload["response"] = text
    return payload


class MessageSizeTooLargeError(Exception):
    """Simulate the producer's measured refusal."""


def bounded_publisher() -> tuple[list[tuple[str, bytes]], Any]:
    published: list[tuple[str, bytes]] = []

    async def publish(topic: str, value: bytes) -> None:
        if len(value) > 1_048_576:
            raise MessageSizeTooLargeError("request exceeds max_request_size")
        published.append((topic, value))

    return published, publish


def test_small_message_is_unchanged() -> None:
    message = {"correlation_id": "small", "nested": ["ok"]}
    reduced, truncated = dlq.bound_dlq_original_message(message)
    assert reduced is message
    assert truncated is False


def test_nested_strings_are_reduced_with_full_string_hash() -> None:
    long = "\u2603" * 300_000
    message = {"correlation_id": "nested", "nested": [{"text": long}, long]}
    reduced, truncated = dlq.bound_dlq_original_message(message)
    assert truncated
    digest = hashlib.sha256(long.encode()).hexdigest()
    for text in (reduced["nested"][0]["text"], reduced["nested"][1]):
        assert text.startswith(long[:512])
        assert "truncated" in text
        assert digest in text
    assert message["nested"][0]["text"] == long


def test_many_strings_fall_back_to_digest_and_identity_scalars() -> None:
    message = {
        "correlation_id": "fallback",
        "session_id": "session",
        "tenant_id": "tenant",
        "task_type": "summarization",
        "status": "completed",
        "many": ["x" * 3000] * 2000,
    }
    reduced, truncated = dlq.bound_dlq_original_message(message)
    assert truncated
    for key in ("correlation_id", "session_id", "tenant_id", "task_type", "status"):
        assert reduced[key] == message[key]
    assert "many" in reduced["dropped_keys"]
    original = json.dumps(message, default=str).encode()
    assert reduced["original_bytes"] == len(original)
    assert reduced["sha256"] == hashlib.sha256(original).hexdigest()
    assert len(json.dumps(reduced).encode()) <= dlq.DLQ_MAX_ENVELOPE_BYTES


def test_normal_envelope_keeps_its_shape_and_message_identity() -> None:
    message = {"correlation_id": "small", "nested": ["ok"]}
    envelope = dlq.build_dlq_envelope(
        original_message=message,
        failure_reason="ordinary failure",
        handler="test",
        correlation_id="small",
    )
    assert set(envelope) == {
        "original_message",
        "failure_reason",
        "correlation_id",
        "retry_count",
        "failed_at",
        "handler",
    }
    assert envelope["original_message"] is message
    assert envelope["failure_reason"] == "ordinary failure"


@pytest.mark.parametrize(
    "message",
    [
        {"text": "x" * 899_800},
        {"many": ["x" * 1000] * 896},
    ],
)
def test_complete_envelope_stays_within_budget(message: dict[str, Any]) -> None:
    envelope = dlq.build_dlq_envelope(
        original_message=message,
        failure_reason="\u2603" * 100_000,
        handler="test",
        correlation_id="budget",
    )
    assert len(envelope["failure_reason"]) <= 4096
    assert len(json.dumps(envelope, default=str).encode()) <= dlq.DLQ_MAX_ENVELOPE_BYTES
    assert envelope["original_message_truncated"] is True


@pytest.mark.asyncio
async def test_route_to_dlq_also_bounds_the_complete_envelope() -> None:
    published, publish = bounded_publisher()
    assert await dlq.route_to_dlq(
        publish=publish,
        dlq_topics=[DELEGATION_DLQ_TOPIC],
        original_message=oversized_poison(),
        failure_reason="failure" * 100_000,
        handler="test",
        correlation_id="route",
    )
    assert len(published) == 1
    assert len(published[0][1]) <= dlq.DLQ_MAX_ENVELOPE_BYTES


@pytest.mark.asyncio
async def test_oversized_poison_is_quarantined_and_committed() -> None:
    published, publish = bounded_publisher()
    runner, rows = recording_runner(publish)
    consumer = _CommitRecordingConsumer()
    runner._consumer = consumer
    payload = oversized_poison()
    msg = _wrapped_msg(runner._topic_delegate_skill_completed, 41, payload)
    assert len(msg.value) > 1_048_576
    await runner._handle_message(msg)
    assert rows == []
    assert len(published) == 1
    topic, value = published[0]
    assert topic == DELEGATION_DLQ_TOPIC
    assert len(value) <= dlq.DLQ_MAX_ENVELOPE_BYTES
    envelope = json.loads(value)
    assert envelope["correlation_id"] == payload["correlation_id"]
    assert envelope["original_message_truncated"] is True
    assert envelope["original_message_bytes"] > 1_048_576
    assert [list(commit.values()) for commit in consumer.commits] == [[42]]
