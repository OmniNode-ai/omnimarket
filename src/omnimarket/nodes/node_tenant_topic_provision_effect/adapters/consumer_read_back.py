# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A consumer's read of topics: which of the named topics a started consumer's own metadata read lists."""

from __future__ import annotations

from collections.abc import Iterable

from aiokafka import AIOKafkaConsumer
from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_for


async def consumer_visible_topics(
    bootstrap_servers: str, topics: Iterable[str]
) -> tuple[str, ...]:
    """The sorted subset of ``topics`` that the consumer's metadata read lists."""
    auth = build_aiokafka_auth_kwargs_for(bootstrap_servers)
    security_protocol = str(auth.pop("security_protocol", "PLAINTEXT"))
    consumer = AIOKafkaConsumer(
        bootstrap_servers=bootstrap_servers,
        security_protocol=security_protocol,
        **auth,
    )
    await consumer.start()
    try:
        known = await consumer.topics()
        return tuple(topic for topic in sorted(set(topics)) if topic in known)
    finally:
        await consumer.stop()


__all__ = ["consumer_visible_topics"]
