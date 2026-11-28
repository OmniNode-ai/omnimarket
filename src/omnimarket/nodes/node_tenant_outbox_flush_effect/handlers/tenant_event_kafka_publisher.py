# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus publisher of the outbox flush: implements ``ProtocolTenantEventPublisher`` over a Kafka producer.

``publish`` returns only after ``send_and_wait`` returned, that is after the broker acknowledged the record, and raises
otherwise, so the handler marks a row only after an ack. The producer is built and started on the first publish by an
injected factory (the overlay supplies the one that dials the broker its ``event_bus`` declares).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol


class ProtocolKafkaProducer(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send_and_wait(
        self, topic: str, *, value: bytes, key: bytes
    ) -> object: ...


class KafkaTenantEventPublisher:
    """One lazily started producer; ``close`` stops it when it was started."""

    def __init__(
        self, producer_factory: Callable[[], Awaitable[ProtocolKafkaProducer]]
    ) -> None:
        self._factory = producer_factory
        self._producer: ProtocolKafkaProducer | None = None

    async def publish(self, topic: str, key: str, value: Mapping[str, Any]) -> None:
        if self._producer is None:
            producer = await self._factory()
            await producer.start()
            self._producer = producer
        await self._producer.send_and_wait(
            topic,
            value=json.dumps(dict(value), sort_keys=True).encode("utf-8"),
            key=key.encode("utf-8"),
        )

    async def close(self) -> None:
        if self._producer is not None:
            producer, self._producer = self._producer, None
            await producer.stop()


__all__ = ["KafkaTenantEventPublisher", "ProtocolKafkaProducer"]
