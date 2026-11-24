# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tenant registry held in this process: the tenants it was given and the state recorded for each."""

from __future__ import annotations

from omnimarket.nodes.node_tenant_topic_provision_effect.models.model_tenant_topic_provision import (
    EnumTenantTopicState,
)


class InMemoryTenantTopicRegistry:
    """Holds slugs and states in memory; a durable registry is injected in its place by a deployment."""

    def __init__(self, slugs: tuple[str, ...] = ()) -> None:
        self._slugs = slugs
        self.states: dict[str, EnumTenantTopicState] = {}
        self.failed_topics: dict[str, tuple[str, ...]] = {}

    async def list_tenant_slugs(self) -> tuple[str, ...]:
        return self._slugs

    async def record_state(
        self,
        tenant_slug: str,
        state: EnumTenantTopicState,
        failed_topics: tuple[str, ...],
    ) -> None:
        self.states[tenant_slug] = state
        self.failed_topics[tenant_slug] = failed_topics


__all__ = ["InMemoryTenantTopicRegistry"]
