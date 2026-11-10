# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only broker topic listing for the provision dry run."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aiokafka.admin import AIOKafkaAdminClient
from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_from_env


@runtime_checkable
class ProtocolBrokerTopicLister(Protocol):
    """A read-only broker topic lister."""

    async def list_topics(self) -> tuple[str, ...]:
        """Return the sorted tuple of topic names that do not start with an underscore."""
        ...


class AiokafkaBrokerTopicLister:
    """Opens an aiokafka AIOKafkaAdminClient and lists topics (read-only)."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap_servers = bootstrap_servers

    async def list_topics(self) -> tuple[str, ...]:
        """List topics, filtering out internal (underscore-prefixed) topics."""
        admin = AIOKafkaAdminClient(
            bootstrap_servers=self._bootstrap_servers,
            **build_aiokafka_auth_kwargs_from_env(),
        )
        await admin.start()
        try:
            names = await admin.list_topics()
        finally:
            await admin.close()
        return tuple(sorted(t for t in names if not t.startswith("_")))


async def list_broker_topics(bootstrap_servers: str) -> tuple[str, ...]:
    """Open an aiokafka admin client, list topics, close, and return sorted non-internal names."""
    lister = AiokafkaBrokerTopicLister(bootstrap_servers)
    return await lister.list_topics()
