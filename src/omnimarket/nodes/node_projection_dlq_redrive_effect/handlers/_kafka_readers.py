# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The redrive's broker reads.

This node's contract declares the Kafka transport. Broker auth comes from the
shared omnibase_infra builder over the lane's standard KAFKA_* environment, so
no credential is read or named here. Both readers assign partitions and seek:
neither joins a consumer group nor commits, so no consumer offset moves.
"""

from __future__ import annotations

import json
import os
from typing import Any

from omnimarket.topic_namespace import apply_topic_namespace

BOOTSTRAP_ENV = "KAFKA_BOOTSTRAP_SERVERS"
_POLL_TIMEOUT_MS = 10_000


async def _consumer(bootstrap: str) -> Any:
    from aiokafka import AIOKafkaConsumer
    from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_from_env

    consumer = AIOKafkaConsumer(
        bootstrap_servers=bootstrap,
        group_id=None,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        **build_aiokafka_auth_kwargs_from_env(),
    )
    await consumer.start()
    return consumer


class LazyKafkaSourceReader:
    """Reads one record by (topic, partition, offset)."""

    async def read_record(
        self, topic: str, partition: int, offset: int
    ) -> bytes | None:
        from aiokafka import TopicPartition

        consumer = await _consumer(os.environ[BOOTSTRAP_ENV])
        try:
            tp = TopicPartition(apply_topic_namespace(topic), partition)
            consumer.assign([tp])
            beginning = (await consumer.beginning_offsets([tp]))[tp]
            end = (await consumer.end_offsets([tp]))[tp]
            if not beginning <= offset < end:
                return None
            consumer.seek(tp, offset)
            batch = await consumer.getmany(
                tp, timeout_ms=_POLL_TIMEOUT_MS, max_records=1
            )
            records = batch.get(tp, [])
            if not records or records[0].offset != offset:
                return None
            value: bytes | None = records[0].value
            return value
        finally:
            await consumer.stop()


class LazyKafkaDeadLetterReader:
    """Reads every envelope on the dead-letter topic, keeping the requested ones."""

    async def read_dead_letters(
        self, dlq_topic: str, correlation_ids: frozenset[str]
    ) -> list[bytes]:
        from aiokafka import TopicPartition

        consumer = await _consumer(os.environ[BOOTSTRAP_ENV])
        kept: list[bytes] = []
        try:
            topic = apply_topic_namespace(dlq_topic)
            await consumer.topics()
            partitions = consumer.partitions_for_topic(topic) or set()
            tps = [TopicPartition(topic, p) for p in sorted(partitions)]
            consumer.assign(tps)
            await consumer.seek_to_beginning(*tps)
            ends = await consumer.end_offsets(tps)
            pending = {tp for tp in tps if await consumer.position(tp) < ends[tp]}
            while pending:
                batch = await consumer.getmany(timeout_ms=_POLL_TIMEOUT_MS)
                if not batch:
                    break
                for tp, records in batch.items():
                    for record in records:
                        if record.value is not None and _wanted(
                            record.value, correlation_ids
                        ):
                            kept.append(record.value)
                    if await consumer.position(tp) >= ends[tp]:
                        pending.discard(tp)
        finally:
            await consumer.stop()
        return kept


def _wanted(value: bytes, correlation_ids: frozenset[str]) -> bool:
    if not correlation_ids:
        return True
    try:
        envelope = json.loads(value)
    except ValueError:
        return False
    return (
        isinstance(envelope, dict) and envelope.get("correlation_id") in correlation_ids
    )
