"""OMN-21288: a projection dead letter names the source record it came from.

A redrive re-reads the refused record from its source topic once the cause is
fixed (a tenant joins the mirror). It can only do that without an offset reset
when the dead-letter envelope carries the record's source topic, partition and
offset, so both routing paths stamp them from the ``MessageMeta`` they hold.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.models.model_message_meta import MessageMeta
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

_SOURCE_KEYS = {"source_topic", "source_partition", "source_offset"}


def _publisher() -> tuple[list[tuple[str, bytes]], Any]:
    published: list[tuple[str, bytes]] = []

    async def publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    return published, publish


def test_envelope_carries_the_source_coordinates() -> None:
    envelope = dlq.build_dlq_envelope(
        original_message={"correlation_id": "c-1"},
        failure_reason="tenant not in mirror",
        handler="test",
        correlation_id="c-1",
        source=MessageMeta(
            partition=3, offset=36340, fallback_id="c-1", topic="onex.evt.x.v1"
        ),
    )
    assert envelope["source_topic"] == "onex.evt.x.v1"
    assert envelope["source_partition"] == 3
    assert envelope["source_offset"] == 36340


def test_envelope_without_a_source_keeps_its_shape() -> None:
    envelope = dlq.build_dlq_envelope(
        original_message={"correlation_id": "c-2"},
        failure_reason="ordinary failure",
        handler="test",
        correlation_id="c-2",
    )
    assert not _SOURCE_KEYS & set(envelope)


@pytest.mark.asyncio
async def test_route_to_dlq_passes_the_source_through() -> None:
    published, publish = _publisher()
    assert await dlq.route_to_dlq(
        publish=publish,
        dlq_topics=[DELEGATION_DLQ_TOPIC],
        original_message={"correlation_id": "c-3"},
        failure_reason="malformed",
        handler="test",
        correlation_id="c-3",
        source=MessageMeta(partition=0, offset=7, fallback_id="c-3", topic="t.v1"),
    )
    envelope = json.loads(published[0][1])
    assert (
        envelope["source_topic"],
        envelope["source_partition"],
        envelope["source_offset"],
    ) == ("t.v1", 0, 7)


@pytest.mark.asyncio
async def test_poison_routed_by_the_runner_names_its_source_record() -> None:
    published, publish = _publisher()
    runner, rows = recording_runner(publish)
    runner._consumer = _CommitRecordingConsumer()
    payload = recorded_terminal()
    payload.pop("tenant_id")
    payload["task_type"] = "summarization"
    topic = runner._topic_delegate_skill_completed
    await runner._handle_message(_wrapped_msg(topic, 41, payload))
    assert rows == []
    assert len(published) == 1
    envelope = json.loads(published[0][1])
    assert envelope["correlation_id"] == payload["correlation_id"]
    assert envelope["source_topic"] == topic
    assert envelope["source_partition"] == 0
    assert envelope["source_offset"] == 41
