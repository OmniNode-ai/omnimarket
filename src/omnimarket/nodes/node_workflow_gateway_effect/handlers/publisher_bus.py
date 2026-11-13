# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real publishers of the gateway's wire messages onto the event bus."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumKafkaAcks
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config.model_kafka_event_bus_config import (
    ModelKafkaEventBusConfig,
)
from omnibase_infra.protocols import ProtocolEventBusLike

from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_publisher import (
    WorkflowPublishError,
)


class BusWorkflowPublisher:
    """Publish a wire message through a shared event bus."""

    def __init__(self, bus: ProtocolEventBusLike) -> None:
        self._bus = bus

    async def publish(
        self,
        *,
        topic: str,
        key: str,
        wire: dict[str, Any],
    ) -> None:
        try:
            envelope: ModelEventEnvelope[object] = (
                ModelEventEnvelope.model_validate_json(json.dumps(wire))
            )
            await self._bus.publish_envelope(
                envelope=envelope, topic=topic, key=key.encode()
            )
        except Exception as exc:
            raise WorkflowPublishError(type(exc).__name__) from exc


class KafkaWorkflowPublisher:
    """Lazy, shared Kafka bus; missing bootstrap servers refuse publication."""

    def __init__(
        self,
        *,
        bootstrap_servers_env: str,
        timeout_seconds: float,
    ) -> None:
        self._bootstrap_servers_env = bootstrap_servers_env
        self._timeout_seconds = timeout_seconds
        self._bus: EventBusKafka | None = None
        self._lock = asyncio.Lock()

    async def _get_bus(self) -> EventBusKafka:
        async with self._lock:
            if self._bus is None:
                bootstrap = os.environ.get(self._bootstrap_servers_env, "").strip()
                if not bootstrap:
                    raise WorkflowPublishError("broker is not configured")
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

    async def publish(
        self,
        *,
        topic: str,
        key: str,
        wire: dict[str, Any],
    ) -> None:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                bus = await self._get_bus()
                await BusWorkflowPublisher(bus).publish(topic=topic, key=key, wire=wire)
        except WorkflowPublishError:
            raise
        except Exception as exc:
            raise WorkflowPublishError(type(exc).__name__) from exc

    async def close(self) -> None:
        async with self._lock:
            if self._bus is not None:
                await self._bus.close()
                self._bus = None


__all__: list[str] = ["BusWorkflowPublisher", "KafkaWorkflowPublisher"]
