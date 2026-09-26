# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Commit-free broker metadata and timestamp reads for topic activity."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

BOOTSTRAP_ENV = "KAFKA_BOOTSTRAP_SERVERS"
_FETCH_TIMEOUT_SECONDS = 5.0
# Partitions per offsets_for_times request; see watermarks_and_offsets_for_times.
_TIME_LOOKUP_CHUNK = 100


@dataclass(frozen=True, slots=True)
class PartitionOffsetSnapshot:
    """Watermarks and time-indexed offsets for one broker partition."""

    low: int
    high: int
    offset_one_hour: int | None
    offset_one_day: int | None


class AiokafkaTopicActivityReader:
    """Group-less reader using the runtime's standard Kafka auth builder."""

    def __init__(self, bootstrap_servers: str = "") -> None:
        self._bootstrap = bootstrap_servers
        self._consumer: Any = None

    async def _client(self) -> Any:
        if not self._bootstrap:
            self._bootstrap = os.environ[BOOTSTRAP_ENV]
        if self._consumer is None:
            from aiokafka import AIOKafkaConsumer
            from omnibase_infra.event_bus.kafka_auth import (
                build_aiokafka_auth_kwargs_from_env,
            )

            self._consumer = AIOKafkaConsumer(
                bootstrap_servers=self._bootstrap,
                group_id=None,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                **build_aiokafka_auth_kwargs_from_env(),
            )
            await self._consumer.start()
        return self._consumer

    async def close(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

    async def topic_partitions(self) -> dict[str, tuple[int, ...]]:
        from aiokafka.admin import AIOKafkaAdminClient
        from omnibase_infra.event_bus.kafka_auth import (
            build_aiokafka_auth_kwargs_from_env,
        )

        await self._client()
        admin = AIOKafkaAdminClient(
            bootstrap_servers=self._bootstrap,
            **build_aiokafka_auth_kwargs_from_env(),
        )
        await admin.start()
        try:
            names = sorted(await admin.list_topics())
            described = await admin.describe_topics(names)
        finally:
            await admin.close()
        return {
            str(entry["topic"]): tuple(
                sorted(int(partition["partition"]) for partition in entry["partitions"])
            )
            for entry in described
            if not entry.get("error_code")
        }

    async def watermarks_and_offsets_for_times(
        self,
        partitions: tuple[tuple[str, int], ...],
        *,
        one_hour_ms: int,
        one_day_ms: int,
    ) -> dict[tuple[str, int], PartitionOffsetSnapshot]:
        """Read watermarks for every partition, then time offsets for non-empty ones.

        Measured on the .201 lab broker (2026-09-26): one ``offsets_for_times``
        call across every partition of all 1,755 topics timed out after 40 s,
        while the same call for one topic returned at once. Only a non-empty
        partition can have a message after a lookback time, so the time lookups
        are restricted to those (about 105 topics on the lab) and issued in
        bounded chunks. An empty partition's time offsets are ``None``, which
        the fold reads as "no message in the window".
        """
        from aiokafka import TopicPartition

        if not partitions:
            return {}
        consumer = await self._client()
        by_key = {
            (topic, partition): TopicPartition(topic, partition)
            for topic, partition in partitions
        }
        topic_partitions = list(by_key.values())
        lows, highs = await asyncio.gather(
            consumer.beginning_offsets(topic_partitions),
            consumer.end_offsets(topic_partitions),
        )
        non_empty = [tp for tp in topic_partitions if int(highs[tp]) > int(lows[tp])]
        hour_offsets: dict[Any, Any] = {}
        day_offsets: dict[Any, Any] = {}
        for start in range(0, len(non_empty), _TIME_LOOKUP_CHUNK):
            chunk = non_empty[start : start + _TIME_LOOKUP_CHUNK]
            hour_chunk, day_chunk = await asyncio.gather(
                consumer.offsets_for_times(dict.fromkeys(chunk, one_hour_ms)),
                consumer.offsets_for_times(dict.fromkeys(chunk, one_day_ms)),
            )
            hour_offsets.update(hour_chunk)
            day_offsets.update(day_chunk)
        snapshots: dict[tuple[str, int], PartitionOffsetSnapshot] = {}
        for key, topic_partition in by_key.items():
            hour = hour_offsets.get(topic_partition)
            day = day_offsets.get(topic_partition)
            snapshots[key] = PartitionOffsetSnapshot(
                low=int(lows[topic_partition]),
                high=int(highs[topic_partition]),
                offset_one_hour=None if hour is None else int(hour.offset),
                offset_one_day=None if day is None else int(day.offset),
            )
        return snapshots

    async def timestamps_at_offsets(
        self, offsets: dict[tuple[str, int], int]
    ) -> dict[tuple[str, int], datetime]:
        """Fetch one timestamp per requested partition with a shared assignment."""
        from aiokafka import TopicPartition

        if not offsets:
            return {}
        consumer = await self._client()
        by_key = {key: TopicPartition(key[0], key[1]) for key in sorted(offsets)}
        by_partition = {partition: key for key, partition in by_key.items()}
        consumer.assign(list(by_partition))
        for key, partition in by_key.items():
            consumer.seek(partition, offsets[key])

        pending = set(by_partition)
        found: dict[tuple[str, int], datetime] = {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _FETCH_TIMEOUT_SECONDS
        while pending:
            remaining_ms = int(max(0.0, deadline - loop.time()) * 1000)
            if remaining_ms <= 0:
                break
            batches = await consumer.getmany(
                *tuple(pending),
                timeout_ms=remaining_ms,
                max_records=max(1, len(pending)),
            )
            progressed = False
            for partition, messages in batches.items():
                if not messages:
                    continue
                key = by_partition[partition]
                target = offsets[key]
                message = next(
                    (candidate for candidate in messages if candidate.offset >= target),
                    None,
                )
                if message is None:
                    continue
                found[key] = datetime.fromtimestamp(message.timestamp / 1000, tz=UTC)
                pending.discard(partition)
                progressed = True
            if not progressed:
                break
        return found


__all__ = [
    "BOOTSTRAP_ENV",
    "AiokafkaTopicActivityReader",
    "PartitionOffsetSnapshot",
]
