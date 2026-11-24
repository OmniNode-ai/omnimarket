# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reader's bus query client, with a reply consumer that joins no consumer group (OMN-26508).

Measured on a lab host (2026-11-24, lane integx-2-0030): one ``lane-liveness`` read took 7.4 s, and
the node answered in 0.26 s of it. The infra query client builds a ``KafkaTransport`` with a fresh
uuid consumer group, and ``start()`` alone took 4.07 s (group join and first-fetch priming). A
reader process makes one read and exits, so it never needs a group: it needs the reply topic's end
as it stood before the request. This client hands the infra handler a transport that assigns the
reply partitions by hand and seeks to the end, with no group, no rebalance and no committed offset.

Imported only from the route factory, so a host that never uses the bus-session route imports
nothing from infra. Only the transport differs; request validation, the correlation checks and the
answer checks stay the infra handler's own.
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Mapping
from typing import Any, Self

from aiokafka.errors import UnknownTopicOrPartitionError
from aiokafka.structs import TopicPartition
from omnibase_infra.enums import EnumKafkaAcks
from omnibase_infra.event_bus.kafka_transport import (
    KafkaTransport,
    create_offset_consumer,
)
from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig
from omnibase_infra.handlers.handler_work_ledger_query import HandlerWorkLedgerQuery
from omnibase_infra.topics.topic_namespace import (
    apply_topic_namespace,
    resolve_topic_namespace,
)


class GrouplessReplyTransport:
    """Publish on the command topic; read replies from the end of the reply topic, groupless."""

    def __init__(self, *, config: ModelKafkaEventBusConfig, reply_topic: str) -> None:
        self._config = config
        self._reply_topic = apply_topic_namespace(
            reply_topic, namespace=resolve_topic_namespace()
        )
        self._producer = KafkaTransport(config=config, topics=())
        self._consumer: Any = None

    async def start(self) -> None:
        """Start the producer, then position the reply consumer at the end of every partition."""
        try:
            await self._producer.start()
            consumer = create_offset_consumer(self._config)
            self._consumer = consumer
            await consumer.start()
            await consumer.topics()
            # Track the reply topic and await the metadata fetch that surfaces its partitions
            # (the same step the infra pattern-B broker takes; aiokafka has no public form).
            client = getattr(consumer, "_client", None)
            set_topics = getattr(client, "set_topics", None)
            if callable(set_topics):
                pending = set_topics([self._reply_topic])
                if inspect.isawaitable(pending):
                    await pending
            partitions = consumer.partitions_for_topic(self._reply_topic)
            if not partitions:
                # A topic this principal cannot see is a refusal, never an empty reply stream.
                raise UnknownTopicOrPartitionError(self._reply_topic)
            assigned = [
                TopicPartition(self._reply_topic, p) for p in sorted(partitions)
            ]
            consumer.assign(assigned)
            await consumer.seek_to_end(*assigned)
        except BaseException:
            await self.close()
            raise

    async def send(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: Mapping[str, bytes],
    ) -> None:
        await self._producer.send(topic, key, value, headers)

    async def poll(self, *, max_messages: int, timeout_ms: int) -> list[Any]:
        """The reply records published since ``start``; each carries ``value``."""
        if self._consumer is None:
            raise RuntimeError("reply transport used before start()")
        batches = await self._consumer.getmany(
            timeout_ms=timeout_ms, max_records=max_messages
        )
        return [record for records in batches.values() for record in records]

    async def close(self) -> None:
        consumer, self._consumer = self._consumer, None
        if consumer is not None:
            with contextlib.suppress(Exception):
                await consumer.stop()
        await self._producer.close()


class GrouplessLedgerQueryClient(HandlerWorkLedgerQuery):
    """The infra work-ledger query client over a transport that joins no consumer group."""

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        transport = GrouplessReplyTransport(
            config=self._transport.model_copy(
                update={"acks": EnumKafkaAcks.ALL, "enable_idempotence": False}
            ),
            reply_topic=self._contract.completed_topic,
        )
        # Held before it starts, so a failed start is closed by the handler's own cleanup.
        holder: Any = self._client
        await holder.hold(transport).start()
        return self


__all__ = ["GrouplessLedgerQueryClient", "GrouplessReplyTransport"]
