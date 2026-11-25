# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The identity store seam of the stability gateway key provision handler (OMN-26586).

Both methods are idempotent. A driver or connection failure is raised as ``TenantStoreUnavailableError`` carrying the class
name only: never a DSN, a statement or a driver message.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from omnimarket.nodes.node_stability_gateway_key_provision_effect.models.model_stability_gateway_key_provision import (
    EnumApiKeyEnsure,
    ModelTenantEnsured,
)


@runtime_checkable
class ProtocolGatewayIdentityStore(Protocol):
    """The tenant and gateway key tables of the identity database."""

    async def ensure_tenant(
        self, *, tenant_slug: str, name: str
    ) -> ModelTenantEnsured: ...

    async def ensure_api_key(
        self,
        *,
        tenant_id: UUID,
        name: str,
        key_hash: str,
        expires_at: datetime | None = None,
    ) -> EnumApiKeyEnsure: ...


__all__ = ["ProtocolGatewayIdentityStore"]
