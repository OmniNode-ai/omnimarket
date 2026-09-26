# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19716 broker sampler tests with a hermetic fake reader."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.events.topic_activity import (
    ModelTopicActivitySample,
    ModelTopicActivitySampleEvent,
)
from omnimarket.nodes.node_topic_activity_sampler_effect.handlers._kafka_topic_activity_reader import (
    PartitionOffsetSnapshot,
)
from omnimarket.nodes.node_topic_activity_sampler_effect.handlers.handler_topic_activity_sampler import (
    HandlerTopicActivitySampler,
    split_sample_events,
)
from omnimarket.nodes.node_topic_activity_sampler_effect.models import (
    ModelTopicActivitySampleTrigger,
)

pytestmark = pytest.mark.unit
_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.monotonic = 0.0

    def tick(self) -> float:
        return self.monotonic


class _FakeReader:
    def __init__(self) -> None:
        self.high = 30
        self.timestamp_reads: list[tuple[str, int, int]] = []

    async def topic_partitions(self) -> dict[str, tuple[int, ...]]:
        return {
            "__consumer_offsets": (0,),
            "empty.topic": (0,),
            "onex.evt.active.v1": (0,),
        }

    async def watermarks_and_offsets_for_times(
        self,
        partitions: tuple[tuple[str, int], ...],
        *,
        one_hour_ms: int,
        one_day_ms: int,
    ) -> dict[tuple[str, int], PartitionOffsetSnapshot]:
        assert one_hour_ms == int((_NOW - timedelta(hours=1)).timestamp() * 1000)
        assert one_day_ms == int((_NOW - timedelta(days=1)).timestamp() * 1000)
        assert all(not topic.startswith("__") for topic, _ in partitions)
        return {
            key: (
                PartitionOffsetSnapshot(5, 5, None, None)
                if key[0] == "empty.topic"
                else PartitionOffsetSnapshot(10, self.high, 20, 12)
            )
            for key in partitions
        }

    async def timestamps_at_offsets(
        self, offsets: dict[tuple[str, int], int]
    ) -> dict[tuple[str, int], datetime]:
        for (topic, partition), offset in offsets.items():
            self.timestamp_reads.append((topic, partition, offset))
        return {
            key: (
                _NOW - timedelta(days=2)
                if offset == 10
                else _NOW - timedelta(seconds=5)
            )
            for key, offset in offsets.items()
        }


def test_sampler_counts_offsets_throttles_and_skips_internal_and_empty_topics() -> None:
    clock = _Clock()
    reader = _FakeReader()
    handler = HandlerTopicActivitySampler(
        reader=reader,
        monotonic=clock.tick,
        clock=lambda: _NOW,
    )
    first = asyncio.run(handler.handle(ModelTopicActivitySampleTrigger()))
    assert len(first.events) == 1
    event = first.events[0]
    assert isinstance(event, ModelTopicActivitySampleEvent)
    assert event.total_topic_count == 2
    assert event.empty_topic_count == 1
    assert event.broker_topics == ("empty.topic", "onex.evt.active.v1")
    assert len(event.topics) == 1
    sample = event.topics[0]
    assert sample.topic == "onex.evt.active.v1"
    assert sample.messages_last_hour == 10
    assert sample.messages_last_24h == 18

    clock.monotonic = 29.0
    assert asyncio.run(handler.handle(ModelTopicActivitySampleTrigger())).events == ()


def test_unchanged_high_watermarks_do_not_reread_newest_records() -> None:
    clock = _Clock()
    reader = _FakeReader()
    handler = HandlerTopicActivitySampler(
        reader=reader,
        monotonic=clock.tick,
        clock=lambda: _NOW,
    )
    asyncio.run(handler.handle(ModelTopicActivitySampleTrigger()))
    first_reads = list(reader.timestamp_reads)
    assert ("onex.evt.active.v1", 0, 29) in first_reads

    clock.monotonic = 31.0
    second = asyncio.run(handler.handle(ModelTopicActivitySampleTrigger()))
    assert reader.timestamp_reads == first_reads
    event = second.events[0]
    assert isinstance(event, ModelTopicActivitySampleEvent)
    assert event.topics[0].newest_message_at is None


def test_event_splitting_keeps_every_part_under_the_contract_bound() -> None:
    samples = tuple(
        ModelTopicActivitySample(
            topic=f"onex.evt.{'x' * 120}.{index}.v1",
            high_watermark_total=index + 1,
            low_watermark_total=0,
            messages_last_hour=index,
            messages_last_24h=index,
            retention_truncated=False,
            newest_message_at=_NOW,
        )
        for index in range(80)
    )
    bound = 15_000
    events = split_sample_events(
        sample_id="sample-1",
        sampled_at=_NOW,
        sample_interval_seconds=30,
        total_topic_count=len(samples),
        empty_topic_count=0,
        broker_topics=tuple(sample.topic for sample in samples),
        samples=samples,
        max_event_bytes=bound,
    )
    assert len(events) > 1
    assert all(len(event.model_dump_json().encode()) < bound for event in events)
    assert [sample.topic for event in events for sample in event.topics] == [
        sample.topic for sample in samples
    ]


class _FailingReader(_FakeReader):
    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    async def watermarks_and_offsets_for_times(
        self,
        partitions: tuple[tuple[str, int], ...],
        *,
        one_hour_ms: int,
        one_day_ms: int,
    ) -> dict[tuple[str, int], PartitionOffsetSnapshot]:
        # The .201 lab broker timed out a whole-broker offsets_for_times in 40 s.
        raise TimeoutError("Failed to get offsets by times in 40000 ms")

    async def close(self) -> None:
        self.closed += 1


def test_a_broker_read_failure_emits_nothing_and_drops_the_client() -> None:
    """A failed sample never raises: its trigger is the node heartbeat, and a
    raise would dead-letter heartbeats. The client is closed so the next
    sample reconnects, and the exposure goes stale, which is the visible signal."""
    reader = _FailingReader()
    handler = HandlerTopicActivitySampler(
        reader=reader, monotonic=_Clock().tick, clock=lambda: _NOW
    )
    output = asyncio.run(handler.handle(ModelTopicActivitySampleTrigger()))
    assert output.events == ()
    assert reader.closed == 1


class _RecordingConsumer:
    """Stands in for AIOKafkaConsumer to record which partitions get time lookups."""

    def __init__(self, empty: set[int]) -> None:
        self.assigned: list[object] = []
        self.empty = empty
        self.time_lookup_sizes: list[int] = []
        self.time_lookup_partitions: set[int] = set()

    def assign(self, tps: list[object]) -> None:
        self.assigned = list(tps)

    async def beginning_offsets(self, tps: list[object]) -> dict[object, int]:
        return dict.fromkeys(tps, 5)

    async def end_offsets(self, tps: list[object]) -> dict[object, int]:
        return {tp: (5 if tp.partition in self.empty else 50) for tp in tps}  # type: ignore[attr-defined]

    async def offsets_for_times(self, query: dict[object, int]) -> dict[object, object]:
        self.time_lookup_sizes.append(len(query))
        self.time_lookup_partitions.update(tp.partition for tp in query)  # type: ignore[attr-defined]
        return dict.fromkeys(query)


def test_time_lookups_cover_only_non_empty_partitions_in_bounded_chunks() -> None:
    from omnimarket.nodes.node_topic_activity_sampler_effect.handlers import (
        _kafka_topic_activity_reader as reader_module,
    )

    empty = set(range(0, 250))
    consumer = _RecordingConsumer(empty)
    reader = reader_module.AiokafkaTopicActivityReader("broker:9092")
    reader._consumer = consumer
    keys = tuple(("t", p) for p in range(0, 480))
    snaps = asyncio.run(
        reader.watermarks_and_offsets_for_times(keys, one_hour_ms=1, one_day_ms=0)
    )
    assert len(snaps) == 480
    assert consumer.time_lookup_partitions == set(range(250, 480))
    assert max(consumer.time_lookup_sizes) <= reader_module._TIME_LOOKUP_CHUNK
    assert snaps[("t", 0)].offset_one_hour is None
    # Metadata for every requested topic is loaded before any offset request:
    # without it, aiokafka timed out 17 of 18 chunks on the lab broker.
    assert len(consumer.assigned) == 480
