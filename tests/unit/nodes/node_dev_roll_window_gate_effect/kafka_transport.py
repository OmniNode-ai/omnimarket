# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A real-broker transport for the runtime's own dispatch, built for the gate's parity tests (OMN-24431).

``RuntimeDispatch`` takes any consumer and producer that answer the pull-based transport surface
(``start``, ``close``, ``poll``, ``commit``, ``nack``, ``send``). The in-memory transport is one; this is the
other, over aiokafka, so the same contract, routing map and handlers run against a Redpanda broker the caller
started for the proof (never the dev lane's).

Isolation on a shared broker: the consumer owns a group of its own and starts at the end of every topic, so
what an earlier run left on the topics is never read; the reader of the gate's outputs does the same.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.runtime.model_transport_message import ModelTransportMessage

PARTITION = 0
READ_QUIET_MS = 1500


class KafkaTransport:
    """Consumer and producer over one broker, one partition per topic."""

    def __init__(
        self, bootstrap: str, *, consume: Sequence[str], observe: Sequence[str]
    ) -> None:
        self._bootstrap = bootstrap
        self._consume = tuple(consume)
        self._observe = tuple(observe)
        self._group = f"dev-roll-gate-parity-{uuid.uuid4().hex}"
        self._producer: Any = None
        self._consumer: Any = None
        self._reader: Any = None
        self._seen: dict[str, list[ModelEventEnvelope[object]]] = {}

    async def start(self) -> None:
        if self._consumer is not None:
            return
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
        from aiokafka.admin import AIOKafkaAdminClient, NewTopic
        from aiokafka.errors import TopicAlreadyExistsError

        admin = AIOKafkaAdminClient(bootstrap_servers=self._bootstrap)
        await admin.start()
        try:
            for topic in {*self._consume, *self._observe}:
                with contextlib.suppress(TopicAlreadyExistsError):
                    await admin.create_topics(
                        [NewTopic(topic, num_partitions=1, replication_factor=1)]
                    )
        finally:
            await admin.close()
        self._producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap)
        await self._producer.start()
        self._consumer = AIOKafkaConsumer(
            bootstrap_servers=self._bootstrap,
            group_id=self._group,
            enable_auto_commit=False,
        )
        await self._consumer.start()
        self._consumer.assign([TopicPartition(t, PARTITION) for t in self._consume])
        await self._consumer.seek_to_end()
        self._reader = AIOKafkaConsumer(
            bootstrap_servers=self._bootstrap, enable_auto_commit=False
        )
        await self._reader.start()
        self._reader.assign([TopicPartition(t, PARTITION) for t in self._observe])
        await self._reader.seek_to_end()

    async def close(self) -> None:
        for client in (self._consumer, self._reader, self._producer):
            if client is not None:
                await client.stop()
        self._consumer = self._reader = self._producer = None

    async def poll(
        self, *, max_messages: int, timeout_ms: int
    ) -> Sequence[ModelTransportMessage]:
        batches = await self._consumer.getmany(
            timeout_ms=timeout_ms, max_records=max_messages
        )
        return [
            ModelTransportMessage(
                topic=record.topic,
                partition=record.partition,
                offset=record.offset,
                key=record.key,
                value=record.value,
                headers=dict(record.headers or ()),
                ack_token=(partition, record.offset),
            )
            for partition, records in batches.items()
            for record in records
        ]

    async def commit(self, message: object) -> None:
        partition, offset = message.ack_token  # type: ignore[attr-defined]
        await self._consumer.commit({partition: offset + 1})

    async def nack(self, message: object) -> None:
        partition, offset = message.ack_token  # type: ignore[attr-defined]
        self._consumer.seek(partition, offset)

    async def send(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: Mapping[str, bytes],
    ) -> None:
        await self._producer.send_and_wait(
            topic, value=value, key=key, headers=list(headers.items())
        )

    async def on(self, topic: str) -> list[ModelEventEnvelope[object]]:
        """Every envelope the observed topics carried since start, once the broker has been quiet."""
        while True:
            batches = await self._reader.getmany(timeout_ms=READ_QUIET_MS)
            if not batches:
                break
            for records in batches.values():
                for record in records:
                    self._seen.setdefault(record.topic, []).append(
                        ModelEventEnvelope[object].model_validate_json(record.value)
                    )
        return list(self._seen.get(topic, []))
