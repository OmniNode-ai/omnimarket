# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The redrive's boundaries: the broker reads, the tenant registry, the projection."""

from __future__ import annotations

from typing import Any, Protocol

from omnimarket.models.model_message_meta import MessageMeta


class ProtocolDeadLetterReader(Protocol):
    """Reads dead-letter envelopes off the dead-letter topic."""

    async def read_dead_letters(
        self, dlq_topic: str, correlation_ids: frozenset[str]
    ) -> list[bytes]:
        """Raw envelope values whose correlation id is in the set; every value
        when the set is empty."""
        ...


class ProtocolSourceRecordReader(Protocol):
    """Reads one record from its source topic.

    Reads by assigned partition and seek. It never joins a consumer group and
    never commits, so no consumer offset moves.
    """

    async def read_record(
        self, topic: str, partition: int, offset: int
    ) -> bytes | None:
        """The record's value, or ``None`` when the broker no longer holds it."""
        ...


class ProtocolTenantRegistry(Protocol):
    async def tenant_is_registered(self, tenant_identity: str | None) -> bool:
        """False when the record names a tenant the registry cannot resolve."""
        ...


class ProtocolRecordProjector(Protocol):
    async def project(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Project one unwrapped record through the topic's projection writer."""
        ...
