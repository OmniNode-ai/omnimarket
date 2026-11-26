# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two ports the flush is built on; both are injected, the handler constructs neither."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from omnimarket.nodes.node_tenant_outbox_flush_effect.models.model_tenant_outbox_flush import (
    ModelOutboxRow,
)


class ProtocolTenantOutboxStore(Protocol):
    """The outbox table. ``fetch_unpublished`` returns oldest first and never a row already marked published."""

    async def fetch_unpublished(self, limit: int) -> Sequence[ModelOutboxRow]: ...

    async def mark_published(self, row_id: str, published_at: datetime) -> None: ...

    async def record_failure(self, row_id: str, error: str) -> None: ...


class ProtocolTenantEventPublisher(Protocol):
    """The bus. ``publish`` returns only once the broker acknowledged the record and raises otherwise."""

    async def publish(self, topic: str, key: str, value: Mapping[str, Any]) -> None: ...


__all__ = ["ProtocolTenantEventPublisher", "ProtocolTenantOutboxStore"]
