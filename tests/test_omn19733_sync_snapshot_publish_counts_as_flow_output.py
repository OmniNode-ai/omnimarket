# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19733: the sync snapshot seam counts an acknowledged delta as output.

The session-replay and work-events writers publish their keyed snapshot deltas
through ``KafkaSnapshotDeltaPublisher`` on the worker thread the runtime runs a
sync projection handler on. Measured on the .201 dev lane at 19:48Z, both read
STALLED (25 in, 0 out) because that seam never reached ``record_flow_output``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from omnimarket.projection.snapshot_publisher import (
    KafkaSnapshotDeltaPublisher,
    ModelSnapshotDeltaMessage,
)

_RECORD = (
    "omnibase_infra.runtime.observability.consumer_flow_counters.record_flow_output"
)
_SNAPSHOT_TOPIC = "onex.snapshot.projection.work.events.v1"
_SOURCE_TOPIC = "onex.evt.omniclaude.tool-executed.v1"
_GROUP = "local.omnimarket.projection_work_events.consume.1.0.0"
_T0 = datetime(2026, 9, 26, 19, 48, tzinfo=UTC)


def _message() -> ModelSnapshotDeltaMessage:
    return ModelSnapshotDeltaMessage(
        topic=_SNAPSHOT_TOPIC, key=b'{"id":"1"}', value=b'{"id":"1"}', headers=()
    )


def _publisher(result: bool) -> KafkaSnapshotDeltaPublisher:
    publisher = KafkaSnapshotDeltaPublisher(bootstrap_servers="broker:9092")

    async def _publish(message: ModelSnapshotDeltaMessage) -> bool:
        return result

    publisher._publish = _publish  # type: ignore[method-assign]
    return publisher


@pytest.mark.unit
def test_acknowledged_sync_publish_records_flow_output_once() -> None:
    with patch(_RECORD) as record:
        assert _publisher(True).publish(_message()) is True
    record.assert_called_once_with(_SNAPSHOT_TOPIC)


@pytest.mark.unit
def test_unacknowledged_sync_publish_records_nothing() -> None:
    with patch(_RECORD) as record:
        assert _publisher(False).publish(_message()) is False
    record.assert_not_called()


@pytest.mark.unit
def test_no_brokers_records_nothing() -> None:
    publisher = KafkaSnapshotDeltaPublisher(bootstrap_servers="  ")
    with patch(_RECORD) as record:
        assert publisher.publish(_message()) is False
    record.assert_not_called()


@pytest.mark.unit
def test_sync_publish_on_a_dispatch_worker_thread_counts_messages_out() -> None:
    """The runtime runs a sync projection handler with ``asyncio.to_thread``,
    which carries the dispatch's contextvars; the delta must land on that
    subscription's window as one output."""
    pytest.importorskip("omnibase_infra.runtime.observability.consumer_flow_counters")
    from omnibase_infra.runtime.observability.consumer_flow_counters import (
        active_flow_key,
        get_consumer_flow_counters,
        reset_consumer_flow_counters,
    )

    reset_consumer_flow_counters()
    try:
        counters = get_consumer_flow_counters()
        carrier = uuid4()
        counters.register(_GROUP, _SOURCE_TOPIC)
        assert counters.drain(node_id=carrier, now=_T0) is None
        publisher = _publisher(True)

        async def dispatch() -> bool:
            with active_flow_key(_GROUP, _SOURCE_TOPIC):
                return await asyncio.to_thread(publisher.publish, _message())

        assert asyncio.run(dispatch()) is True
        window = counters.drain(node_id=carrier, now=_T0 + timedelta(seconds=30))
        assert window is not None
        rows = [
            d
            for d in window.consumer_deltas
            if d.consumer_group == _GROUP and d.topic == _SOURCE_TOPIC
        ]
        assert len(rows) == 1
        assert rows[0].messages_out == 1
    finally:
        reset_consumer_flow_counters()
