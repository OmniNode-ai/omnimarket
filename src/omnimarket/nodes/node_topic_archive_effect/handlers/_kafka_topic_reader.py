# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The archive effect's broker read: a group-less, commit-free topic reader.

Reads by explicit partition assignment with no consumer group, so it commits
nothing and changes no group state on the broker. This node's contract
declares the Kafka transport. Broker auth comes from the shared omnibase_infra
builder over the lane's standard KAFKA_* environment, the same one every
runtime client uses, so no credential is read or named here.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from omnimarket.topic_archive.models import ModelArchivedRecord

_FETCH_TIMEOUT_MS = 5000
_EMPTY_POLLS_BEFORE_GIVING_UP = 6
#: The broker address for runtime dispatch, read when first used.
BOOTSTRAP_ENV = "KAFKA_BOOTSTRAP_SERVERS"


class AiokafkaTopicReader:
    """Group-less, commit-free reader over explicit partition assignment."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers
        self._consumer: Any = None

    async def _client(self) -> Any:
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

    async def partitions(self, topic: str) -> list[int]:
        # The consumer's partitions_for_topic() answers from metadata it
        # already holds and returns None for a topic it has never been
        # assigned, which read as "no partitions" and archived nothing in the
        # first live run. Ask the broker through the admin API instead.
        from aiokafka.admin import AIOKafkaAdminClient
        from omnibase_infra.event_bus.kafka_auth import (
            build_aiokafka_auth_kwargs_from_env,
        )

        await self._client()  # resolves the bootstrap address
        admin = AIOKafkaAdminClient(
            bootstrap_servers=self._bootstrap,
            **build_aiokafka_auth_kwargs_from_env(),
        )
        await admin.start()
        try:
            described = await admin.describe_topics([topic])
        finally:
            await admin.close()
        found: list[int] = []
        for entry in described:
            if entry.get("topic") == topic and not entry.get("error_code"):
                found.extend(int(p["partition"]) for p in entry.get("partitions", []))
        return sorted(found)

    async def watermarks(self, topic: str, partition: int) -> tuple[int, int]:
        from aiokafka import TopicPartition

        consumer = await self._client()
        tp = TopicPartition(topic, partition)
        low = (await consumer.beginning_offsets([tp]))[tp]
        high = (await consumer.end_offsets([tp]))[tp]
        return int(low), int(high)

    async def read_range(
        self, topic: str, partition: int, start: int, end_exclusive: int
    ) -> AsyncIterator[ModelArchivedRecord]:
        from aiokafka import TopicPartition

        consumer = await self._client()
        tp = TopicPartition(topic, partition)
        consumer.assign([tp])
        low, _high = await self.watermarks(topic, partition)
        consumer.seek(tp, max(start, low))
        empty = 0
        while await consumer.position(tp) < end_exclusive:
            batch = await consumer.getmany(
                tp, timeout_ms=_FETCH_TIMEOUT_MS, max_records=2000
            )
            msgs = batch.get(tp, [])
            if not msgs:
                empty += 1
                if empty >= _EMPTY_POLLS_BEFORE_GIVING_UP:
                    return
                continue
            empty = 0
            for m in msgs:
                if m.offset >= end_exclusive:
                    return
                yield ModelArchivedRecord.from_bytes(
                    topic=topic,
                    partition=partition,
                    offset=m.offset,
                    timestamp_ms=m.timestamp,
                    key=m.key,
                    value=m.value,
                    headers=[(k, v) for k, v in (m.headers or ())],
                )


class LazyKafkaTopicReader(AiokafkaTopicReader):
    """Resolves the broker address from the environment on first use."""

    def __init__(self) -> None:
        super().__init__("")

    async def _client(self) -> Any:
        if not self._bootstrap:
            self._bootstrap = os.environ[BOOTSTRAP_ENV]
        return await super()._client()
