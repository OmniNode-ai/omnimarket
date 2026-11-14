# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The redrive's projection and tenant boundary: the delegation writer itself.

A redriven record goes through ``DelegationProjectionRunner.project_event``, the
same entry the live consumer calls, with the writer's own database bindings, so
a redrive writes exactly what the first projection would have written and
resolves the tenant through the registry the writer resolves it through.
"""

from __future__ import annotations

from typing import Any

from omnimarket.models.model_message_meta import MessageMeta
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
    async_resolve_write_tenant_uuid,
)


class DelegationRedriveBoundary:
    def __init__(self, runner: DelegationProjectionRunner | None = None) -> None:
        """``runner`` is a writer whose database the caller already opened; the
        boundary then neither opens nor closes it."""
        self._runner: DelegationProjectionRunner | None = runner
        self._owns_runner = runner is None

    async def _connected(self) -> DelegationProjectionRunner:
        if self._runner is None:
            runner = DelegationProjectionRunner()
            await runner.open_databases()
            self._runner = runner
        return self._runner

    async def tenant_is_registered(self, tenant_identity: str | None) -> bool:
        runner = await self._connected()
        try:
            await async_resolve_write_tenant_uuid(runner.db, tenant_identity)
        except TenantRegistryResolutionError:
            return False
        return True

    async def project(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        runner = await self._connected()
        return await runner.project_event(topic, data, meta)

    async def close(self) -> None:
        if self._runner is not None and self._owns_runner:
            await self._runner.close_databases()
            self._runner = None
