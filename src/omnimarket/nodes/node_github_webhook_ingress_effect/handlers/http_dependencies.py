# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay-backed dependencies for the contract-declared webhook mount."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumKafkaAcks
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config.model_kafka_event_bus_config import (
    ModelKafkaEventBusConfig,
)
from omnibase_infra.protocols import ProtocolEventBusLike
from pydantic import ValidationError

from omnimarket.nodes.node_github_webhook_ingress_effect.models.model_github_webhook_http import (
    ModelWebhookHttpConfig,
    ModelWebhookTarget,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_http_config() -> ModelWebhookHttpConfig:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    config = ModelWebhookHttpConfig.model_validate(contract["http_ingress"])
    if config.delivery_topic not in contract["event_bus"]["subscribe_topics"]:
        raise ValueError("HTTP delivery topic must be the node's declared input")
    return config


class EnvironmentWebhookConfiguration:
    """Read only the deployment value names declared in the contract."""

    def __init__(self, config: ModelWebhookHttpConfig) -> None:
        self._names = config.environment

    async def resolve(self) -> tuple[bytes, ModelWebhookTarget | None]:
        secret = os.environ.get(self._names["secret"], "").strip().encode()
        try:
            target = ModelWebhookTarget.model_validate(
                {
                    name: os.environ.get(self._names[name], "").strip()
                    for name in ("tenant_id", "tenant_slug", "principal_id")
                }
            )
        except ValidationError:
            target = None
        return secret, target


class BusWebhookPublisher:
    """The same publish boundary works with in-memory and Kafka event buses."""

    def __init__(self, bus: ProtocolEventBusLike) -> None:
        self._bus = bus

    async def publish(self, *, topic: str, key: bytes, value: bytes) -> None:
        envelope: ModelEventEnvelope[object] = ModelEventEnvelope.model_validate_json(
            value
        )
        await self._bus.publish_envelope(envelope=envelope, topic=topic, key=key)


class KafkaWebhookPublisher:
    """Lazy, shared canonical bus; missing overlay config refuses publication."""

    def __init__(self, config: ModelWebhookHttpConfig) -> None:
        self._config = config
        self._bus: EventBusKafka | None = None
        self._lock = asyncio.Lock()

    async def _get_bus(self) -> EventBusKafka:
        async with self._lock:
            if self._bus is None:
                bootstrap = os.environ.get(
                    self._config.environment["bootstrap_servers"], ""
                ).strip()
                if not bootstrap:
                    raise RuntimeError("webhook broker overlay is not configured")
                config = ModelKafkaEventBusConfig(
                    bootstrap_servers=bootstrap
                ).apply_environment_overrides()
                # An overlay cannot weaken the HTTP route's all-replica ack.
                config = config.model_copy(update={"acks": EnumKafkaAcks.ALL})
                bus = EventBusKafka(config)
                try:
                    await bus.start()
                except BaseException:
                    await bus.close()
                    raise
                self._bus = bus
            return self._bus

    async def publish(self, *, topic: str, key: bytes, value: bytes) -> None:
        bus = await self._get_bus()
        await BusWebhookPublisher(bus).publish(topic=topic, key=key, value=value)

    async def close(self) -> None:
        async with self._lock:
            if self._bus is not None:
                await self._bus.close()
                self._bus = None
