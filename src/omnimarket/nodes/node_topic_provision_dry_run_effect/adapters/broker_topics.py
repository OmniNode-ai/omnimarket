# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Broker topic listing for the provision dry run, and the one create the writer topic apply makes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable

from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError
from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_for


@runtime_checkable
class ProtocolBrokerTopicLister(Protocol):
    """A read-only broker topic lister."""

    async def list_topics(self) -> tuple[str, ...]:
        """Return the sorted tuple of topic names that do not start with an underscore."""
        ...


@runtime_checkable
class ProtocolBrokerTopicCreator(Protocol):
    """A broker topic creator that accepts a topic that already exists."""

    async def create_topic(
        self, name: str, num_partitions: int, replication_factor: int
    ) -> bool:
        """Create the topic; True when this call created it, False when it already existed."""
        ...


@asynccontextmanager
async def _admin_client(bootstrap_servers: str) -> AsyncIterator[Any]:
    """A started admin client for this broker, closed on exit."""
    # The lane-aware resolver answers for THIS broker: a bound lane transport when the
    # caller opened one, else the standard runtime Kafka environment.
    auth = build_aiokafka_auth_kwargs_for(bootstrap_servers)
    security_protocol = str(auth.pop("security_protocol", "PLAINTEXT"))
    admin = AIOKafkaAdminClient(
        bootstrap_servers=bootstrap_servers,
        security_protocol=security_protocol,
        **auth,
    )
    await admin.start()
    try:
        yield admin
    finally:
        await admin.close()


class AiokafkaBrokerTopicCreator:
    """Opens an aiokafka AIOKafkaAdminClient and creates one topic (the only write here)."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap_servers = bootstrap_servers

    async def create_topic(
        self, name: str, num_partitions: int, replication_factor: int
    ) -> bool:
        """Create the topic; an existing one is accepted and reported as not created."""
        try:
            async with _admin_client(self._bootstrap_servers) as admin:
                await admin.create_topics(
                    [
                        NewTopic(
                            name=name,
                            num_partitions=num_partitions,
                            replication_factor=replication_factor,
                        )
                    ]
                )
        except TopicAlreadyExistsError:
            return False
        return True


class AiokafkaBrokerTopicLister:
    """Opens an aiokafka AIOKafkaAdminClient and lists topics (read-only)."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap_servers = bootstrap_servers

    async def list_topics(self) -> tuple[str, ...]:
        """List topics, filtering out internal (underscore-prefixed) topics."""
        async with _admin_client(self._bootstrap_servers) as admin:
            names = await admin.list_topics()
        return tuple(sorted(t for t in names if not t.startswith("_")))


async def list_broker_topics(bootstrap_servers: str) -> tuple[str, ...]:
    """Open an aiokafka admin client, list topics, close, and return sorted non-internal names."""
    lister = AiokafkaBrokerTopicLister(bootstrap_servers)
    return await lister.list_topics()
