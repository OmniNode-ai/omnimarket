"""OMN-19733: runner-owned snapshot publishes count as flow output."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    ModelProjectionRuntimeBinding,
)

_SNAPSHOT_TOPIC = "onex.snapshot.projection.test-omn19733-flow-output.v1"
_SOURCE_TOPIC = "onex.evt.omnimarket.test-omn19733-source.v1"
_CONSUMER_GROUP = "local.omnimarket.test-omn19733-snapshot-writer"
_T0 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


class _Runner(BaseProjectionRunner):
    @property
    def topics(self) -> list[str]:
        return [_SOURCE_TOPIC]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        return True


class _Producer:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.send_and_wait = AsyncMock(side_effect=self._send_and_wait)

    async def _send_and_wait(self, *args: Any, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error


def _runner() -> _Runner:
    return _Runner(
        runtime_binding=ModelProjectionRuntimeBinding(
            kafka_bootstrap_servers="kafka.test:9092",
            kafka_consumer_group=_CONSUMER_GROUP,
            database_url="postgresql://projection:secret@db.test:5432/projections",
        )
    )


def _exposure() -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=_SNAPSHOT_TOPIC,
        table="test_omn19733_snapshot_rows",
        columns=("id", "value"),
        bus_backed=True,
        key_columns=("id",),
    )


async def _publish(runner: _Runner) -> bool:
    return await runner.publish_snapshot_delta(
        _exposure(),
        op="upsert",
        row={"id": "row-1", "value": "present"},
        source_event_id="source-1",
        source_topic=_SOURCE_TOPIC,
        source_partition=0,
        source_offset=1,
    )


async def _publish_delete(runner: _Runner) -> bool:
    return await runner.publish_snapshot_delta(
        _exposure(),
        op="delete",
        row=None,
        key={"id": "row-1"},
        source_event_id="source-1",
        source_topic=_SOURCE_TOPIC,
        source_partition=0,
        source_offset=1,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_acknowledged_snapshot_publish_records_flow_output_once() -> None:
    runner = _runner()
    runner._producer = _Producer()  # type: ignore[assignment]

    with patch(
        "omnibase_infra.runtime.observability.consumer_flow_counters.record_flow_output"
    ) as record_flow_output:
        assert await _publish(runner) is True

    record_flow_output.assert_called_once_with(_SNAPSHOT_TOPIC)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_producer_does_not_record_flow_output() -> None:
    runner = _runner()
    runner._ensure_producer = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with patch(
        "omnibase_infra.runtime.observability.consumer_flow_counters.record_flow_output"
    ) as record_flow_output:
        assert await _publish(runner) is False

    record_flow_output.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_snapshot_send_does_not_record_flow_output() -> None:
    runner = _runner()
    runner._producer = _Producer(error=RuntimeError("broker unavailable"))  # type: ignore[assignment]

    with (
        patch(
            "omnibase_infra.runtime.observability.consumer_flow_counters.record_flow_output"
        ) as record_flow_output,
        pytest.raises(RuntimeError, match="broker unavailable"),
    ):
        await _publish(runner)

    record_flow_output.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_acknowledged_snapshot_publish_in_active_flow_counts_messages_out() -> (
    None
):
    from omnibase_infra.runtime.observability.consumer_flow_counters import (
        active_flow_key,
        get_consumer_flow_counters,
        reset_consumer_flow_counters,
    )

    reset_consumer_flow_counters()
    try:
        counters = get_consumer_flow_counters()
        carrier = uuid4()
        counters.register(_CONSUMER_GROUP, _SOURCE_TOPIC)
        assert counters.drain(node_id=carrier, now=_T0) is None

        runner = _runner()
        runner._producer = _Producer()  # type: ignore[assignment]
        with active_flow_key(_CONSUMER_GROUP, _SOURCE_TOPIC):
            assert await _publish_delete(runner) is True

        window = counters.drain(node_id=carrier, now=_T0 + timedelta(seconds=30))
        assert window is not None
        deltas = {
            (delta.consumer_group, delta.topic): delta
            for delta in window.consumer_deltas
        }
        assert deltas[(_CONSUMER_GROUP, _SOURCE_TOPIC)].messages_out == 1
    finally:
        reset_consumer_flow_counters()
