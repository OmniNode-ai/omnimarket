# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publisher of the flush over the shared Kafka event bus: the record is acknowledged by every replica before it returns."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from omnibase_infra.enums import EnumKafkaAcks
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config.model_kafka_event_bus_config import (
    ModelKafkaEventBusConfig,
)


class KafkaTenantEventPublisher:
    """Lazy, shared Kafka bus; a missing broker address refuses publication."""

    def __init__(self, *, bootstrap_servers_env: str, timeout_seconds: float) -> None:
        self._bootstrap_servers_env = bootstrap_servers_env
        self._timeout_seconds = timeout_seconds
        self._bus: EventBusKafka | None = None
        self._lock = asyncio.Lock()

    async def _get_bus(self) -> EventBusKafka:
        async with self._lock:
            if self._bus is None:
                bootstrap = os.environ.get(self._bootstrap_servers_env, "").strip()
                if not bootstrap:
                    raise RuntimeError("broker is not configured")
                config = ModelKafkaEventBusConfig(
                    bootstrap_servers=bootstrap
                ).apply_environment_overrides()
                config = config.model_copy(update={"acks": EnumKafkaAcks.ALL})
                bus = EventBusKafka(config)
                try:
                    await bus.start()
                except BaseException:
                    await bus.close()
                    raise
                self._bus = bus
            return self._bus

    async def publish(self, *, topic: str, key: str, record: dict[str, Any]) -> None:
        async with asyncio.timeout(self._timeout_seconds):
            bus = await self._get_bus()
            await bus.publish(
                topic, key.encode(), json.dumps(record, sort_keys=True).encode()
            )

    async def close(self) -> None:
        async with self._lock:
            if self._bus is not None:
                await self._bus.close()
                self._bus = None


__all__ = ["KafkaTenantEventPublisher"]
