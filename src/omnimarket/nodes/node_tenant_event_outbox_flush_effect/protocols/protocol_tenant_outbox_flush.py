# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the flush: the outbox store and the tenant-topic publisher."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from omnimarket.nodes.node_tenant_event_outbox_flush_effect.models.model_tenant_outbox_flush import (
    ModelOutboxRow,
)


class TenantOutboxStoreError(RuntimeError):
    """The identity database could not be reached or refused the statement; carries the class name only."""


@runtime_checkable
class ProtocolTenantOutboxStore(Protocol):
    """The unpublished rows of the outbox, oldest first, and the two ways a row leaves that set."""

    async def pending(self, limit: int) -> list[ModelOutboxRow]:
        """Up to ``limit`` rows with no ``published_at``, by id."""

    async def mark_published(self, row_id: int) -> None:
        """Stamp ``published_at`` on the row."""

    async def mark_failed(self, row_id: int, error_class: str) -> None:
        """Count one attempt and keep the failing class name (never a message) on the row."""


@runtime_checkable
class ProtocolTenantEventPublisher(Protocol):
    """Publish one envelope to its topic; raises on every failure path."""

    async def publish(self, *, topic: str, key: str, record: dict[str, Any]) -> None:
        """Return only when the broker acknowledged the record."""


__all__ = [
    "ProtocolTenantEventPublisher",
    "ProtocolTenantOutboxStore",
    "TenantOutboxStoreError",
]
