# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams the handler is given. None of them has a default value: every one is injected."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_tenant_topic_provision_effect.models.model_tenant_topic_provision import (
    EnumTenantTopicState,
)


@runtime_checkable
class ProtocolMirrorTopicSource(Protocol):
    """Where the bare topic names of the mirror set come from."""

    def mirror_topic_names(self) -> tuple[str, ...]:
        """The de-duplicated mirror set; an unreadable source raises, never answers empty."""
        ...


@runtime_checkable
class ProtocolTenantTopicRegistry(Protocol):
    """The tenants that exist, and the topic state recorded for each."""

    async def list_tenant_slugs(self) -> tuple[str, ...]:
        """Every tenant slug a backfill covers."""
        ...

    async def record_state(
        self,
        tenant_slug: str,
        state: EnumTenantTopicState,
        failed_topics: tuple[str, ...],
    ) -> None:
        """Record whether the tenant's topic set is whole, and the topics that failed when it is not."""
        ...


__all__ = ["ProtocolMirrorTopicSource", "ProtocolTenantTopicRegistry"]
