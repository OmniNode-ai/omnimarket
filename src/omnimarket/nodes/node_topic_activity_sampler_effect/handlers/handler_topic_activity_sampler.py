# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sample broker topic activity on the existing runtime heartbeat."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.events.topic_activity import (
    ModelTopicActivitySample,
    ModelTopicActivitySampleEvent,
)
from omnimarket.nodes.node_topic_activity_sampler_effect.handlers._kafka_topic_activity_reader import (
    AiokafkaTopicActivityReader,
    PartitionOffsetSnapshot,
)
from omnimarket.nodes.node_topic_activity_sampler_effect.models import (
    ModelTopicActivitySampleTrigger,
    ModelTopicActivitySamplingPolicy,
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_ONE_HOUR = timedelta(hours=1)
_ONE_DAY = timedelta(hours=24)
_PART_COORDINATE_SENTINEL = 999_999
_HANDLER_ID = "node_topic_activity_sampler_effect"


_logger = logging.getLogger(__name__)


class ProtocolTopicActivityReader(Protocol):
    async def topic_partitions(self) -> dict[str, tuple[int, ...]]: ...

    async def watermarks_and_offsets_for_times(
        self,
        partitions: tuple[tuple[str, int], ...],
        *,
        one_hour_ms: int,
        one_day_ms: int,
    ) -> dict[tuple[str, int], PartitionOffsetSnapshot]: ...

    async def timestamps_at_offsets(
        self, offsets: dict[tuple[str, int], int]
    ) -> dict[tuple[str, int], datetime]: ...


def load_sampling_policy(
    path: Path = _CONTRACT_PATH,
) -> ModelTopicActivitySamplingPolicy:
    with open(path) as handle:
        raw = yaml.safe_load(handle)
    return ModelTopicActivitySamplingPolicy.model_validate(raw["sampling_policy"])


def _event_size(event: ModelTopicActivitySampleEvent) -> int:
    return len(event.model_dump_json().encode("utf-8"))


def split_sample_events(
    *,
    sample_id: str,
    sampled_at: datetime,
    sample_interval_seconds: int,
    total_topic_count: int,
    empty_topic_count: int,
    broker_topics: tuple[str, ...],
    samples: tuple[ModelTopicActivitySample, ...],
    max_event_bytes: int,
) -> tuple[ModelTopicActivitySampleEvent, ...]:
    """Greedily split a sample while keeping every serialized part below its cap."""

    def build(
        rows: tuple[ModelTopicActivitySample, ...],
        *,
        part_index: int,
        part_count: int,
    ) -> ModelTopicActivitySampleEvent:
        return ModelTopicActivitySampleEvent(
            sample_id=sample_id,
            sampled_at=sampled_at,
            part_index=part_index,
            part_count=part_count,
            sample_interval_seconds=sample_interval_seconds,
            total_topic_count=total_topic_count,
            empty_topic_count=empty_topic_count,
            broker_topics=broker_topics,
            topics=rows,
        )

    if _event_size(build((), part_index=0, part_count=1)) >= max_event_bytes:
        raise ValueError("topic inventory alone exceeds max_event_bytes")

    chunks: list[tuple[ModelTopicActivitySample, ...]] = []
    current: tuple[ModelTopicActivitySample, ...] = ()
    for sample in samples:
        candidate = (*current, sample)
        pessimistic = build(
            candidate,
            part_index=_PART_COORDINATE_SENTINEL,
            part_count=_PART_COORDINATE_SENTINEL + 1,
        )
        if _event_size(pessimistic) < max_event_bytes:
            current = candidate
            continue
        if not current:
            raise ValueError(
                f"sample for topic {sample.topic!r} exceeds max_event_bytes"
            )
        chunks.append(current)
        current = (sample,)
    if current or not chunks:
        chunks.append(current)

    part_count = len(chunks)
    events = tuple(
        build(rows, part_index=index, part_count=part_count)
        for index, rows in enumerate(chunks)
    )
    if any(_event_size(event) >= max_event_bytes for event in events):
        raise ValueError("final topic-activity event exceeds max_event_bytes")
    return events


class HandlerTopicActivitySampler:
    """Read broker watermarks and return typed sample events for runtime publish."""

    def __init__(
        self,
        *,
        reader: ProtocolTopicActivityReader | None = None,
        monotonic: Callable[[], float] | None = None,
        clock: Callable[[], datetime] | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._reader = reader or AiokafkaTopicActivityReader()
        self._monotonic = monotonic or time.monotonic
        self._clock = clock or (lambda: datetime.now(UTC))
        self._policy = load_sampling_policy(contract_path or _CONTRACT_PATH)
        self._last_sample_monotonic: float | None = None
        self._high_watermarks: dict[tuple[str, int], int] = {}
        self._newest_timestamps: dict[tuple[str, int], datetime] = {}
        self._low_watermarks: dict[tuple[str, int], int] = {}
        self._oldest_timestamps: dict[tuple[str, int], datetime] = {}

    async def handle(
        self, request: ModelTopicActivitySampleTrigger
    ) -> ModelHandlerOutput[None]:
        """Sample at most once per declared interval in this process."""
        del request
        invocation_id = uuid4()
        monotonic_now = self._monotonic()
        if (
            self._last_sample_monotonic is not None
            and monotonic_now - self._last_sample_monotonic
            < self._policy.sample_interval_seconds
        ):
            return ModelHandlerOutput.for_effect(
                input_envelope_id=invocation_id,
                correlation_id=invocation_id,
                handler_id=_HANDLER_ID,
                events=(),
            )
        self._last_sample_monotonic = monotonic_now

        sampled_at = self._clock()
        if sampled_at.tzinfo is None:
            sampled_at = sampled_at.replace(tzinfo=UTC)
        try:
            partitions_by_topic = await self._reader.topic_partitions()
            visible = {
                topic: partitions
                for topic, partitions in partitions_by_topic.items()
                if not topic.startswith("__")
            }
            samples: list[ModelTopicActivitySample] = []
            empty_topic_count = 0
            live_partition_keys = {
                (topic, partition)
                for topic, partitions in visible.items()
                for partition in partitions
            }
            one_hour_ms = int((sampled_at - _ONE_HOUR).timestamp() * 1000)
            one_day_ms = int((sampled_at - _ONE_DAY).timestamp() * 1000)
            offsets = await self._reader.watermarks_and_offsets_for_times(
                tuple(sorted(live_partition_keys)),
                one_hour_ms=one_hour_ms,
                one_day_ms=one_day_ms,
            )
            oldest_requests: dict[tuple[str, int], int] = {}
            newest_requests: dict[tuple[str, int], int] = {}
            for key, snapshot in offsets.items():
                if snapshot.high <= snapshot.low:
                    self._newest_timestamps.pop(key, None)
                    self._oldest_timestamps.pop(key, None)
                    continue
                if (
                    self._low_watermarks.get(key) != snapshot.low
                    or key not in self._oldest_timestamps
                ):
                    oldest_requests[key] = snapshot.low
                if self._high_watermarks.get(key) != snapshot.high:
                    newest_requests[key] = snapshot.high - 1

            self._oldest_timestamps.update(
                await self._reader.timestamps_at_offsets(oldest_requests)
            )
            self._newest_timestamps.update(
                await self._reader.timestamps_at_offsets(newest_requests)
            )
        except Exception:
            # A broker read failure must not raise: this handler is triggered by
            # the node heartbeat, and a raise would dead-letter heartbeats. It is
            # logged at ERROR (the runtime log bridge carries it to the bus) and
            # the exposure goes stale past its declared interval, which is how the
            # Lab tab shows it. The client is dropped so the next sample reconnects.
            _logger.exception(
                "topic-activity sample failed; dropping the broker client"
            )
            close = getattr(self._reader, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    _logger.exception("topic-activity reader close failed")
            return ModelHandlerOutput.for_effect(
                input_envelope_id=invocation_id,
                correlation_id=invocation_id,
                handler_id=_HANDLER_ID,
                events=(),
            )
        for topic in sorted(visible):
            sample = self._sample_topic(topic, visible[topic], sampled_at, offsets)
            if sample is None:
                empty_topic_count += 1
            else:
                samples.append(sample)

        self._discard_missing_partition_cache(live_partition_keys)
        events = split_sample_events(
            sample_id=str(invocation_id),
            sampled_at=sampled_at,
            sample_interval_seconds=self._policy.sample_interval_seconds,
            total_topic_count=len(visible),
            empty_topic_count=empty_topic_count,
            broker_topics=tuple(sorted(visible)),
            samples=tuple(samples),
            max_event_bytes=self._policy.max_event_bytes,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=invocation_id,
            correlation_id=invocation_id,
            handler_id=_HANDLER_ID,
            events=events,
        )

    def _sample_topic(
        self,
        topic: str,
        partitions: tuple[int, ...],
        sampled_at: datetime,
        offsets: dict[tuple[str, int], PartitionOffsetSnapshot],
    ) -> ModelTopicActivitySample | None:
        high_total = 0
        low_total = 0
        last_hour = 0
        last_day = 0
        newest_changed = False
        truncated = False

        for partition in partitions:
            key = (topic, partition)
            snapshot = offsets[key]
            low = snapshot.low
            high = snapshot.high
            low_total += low
            high_total += high
            if high <= low:
                self._high_watermarks[key] = high
                self._low_watermarks[key] = low
                self._newest_timestamps.pop(key, None)
                self._oldest_timestamps.pop(key, None)
                continue

            offset_hour = snapshot.offset_one_hour
            offset_day = snapshot.offset_one_day
            last_hour += 0 if offset_hour is None else high - max(low, offset_hour)
            last_day += 0 if offset_day is None else high - max(low, offset_day)

            oldest = self._oldest_timestamps.get(key)
            if oldest is not None and oldest > sampled_at - _ONE_DAY:
                truncated = True
            self._low_watermarks[key] = low

            if self._high_watermarks.get(key) != high:
                newest_changed = True
            self._high_watermarks[key] = high

        if high_total <= low_total:
            return None
        cached_newest = [
            timestamp
            for (cached_topic, _), timestamp in self._newest_timestamps.items()
            if cached_topic == topic
        ]
        return ModelTopicActivitySample(
            topic=topic,
            high_watermark_total=high_total,
            low_watermark_total=low_total,
            messages_last_hour=last_hour,
            messages_last_24h=last_day,
            retention_truncated=truncated,
            newest_message_at=(
                max(cached_newest) if newest_changed and cached_newest else None
            ),
        )

    def _discard_missing_partition_cache(
        self, live_partition_keys: set[tuple[str, int]]
    ) -> None:
        for cache in (
            self._high_watermarks,
            self._newest_timestamps,
            self._low_watermarks,
            self._oldest_timestamps,
        ):
            for key in tuple(cache):
                if key not in live_partition_keys:
                    del cache[key]


__all__ = [
    "HandlerTopicActivitySampler",
    "ProtocolTopicActivityReader",
    "load_sampling_policy",
    "split_sample_events",
]
