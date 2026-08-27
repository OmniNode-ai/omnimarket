# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Integration-only fixtures with explicit Kafka topic provisioning."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest_asyncio
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs

from tests.helpers.kafka_topic_lifecycle import (
    KafkaSmokeTopic,
    KafkaSmokeTopicLease,
)


@pytest_asyncio.fixture
async def kafka_smoke_topic(
    kafka_integration_bus: EventBusKafka,
) -> AsyncGenerator[KafkaSmokeTopic, None]:
    """Provision one unique smoke topic before consumer subscription.

    The admin client receives the exact bootstrap and auth configuration used
    by ``kafka_integration_bus``.  This keeps the test lane contract-driven
    and avoids a second environment-based broker resolution path.
    """
    config = kafka_integration_bus.config
    lease = KafkaSmokeTopicLease(
        bootstrap_servers=config.bootstrap_servers,
        auth_kwargs=build_aiokafka_auth_kwargs(config),
    )
    async with lease:
        yield lease
