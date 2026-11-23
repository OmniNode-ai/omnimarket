# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The identity store seam over the tenant provision node's Postgres store (OMN-26586).

The tenant is created by that node's own transaction (tenant row, default plan row, outbox envelope), under a command id
derived from the slug so a second run replays the first. The key row is the store's ``ensure_api_key``.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.events.tenant_provision_command import ModelTenantProvisionCommand
from omnimarket.nodes.node_stability_gateway_key_provision_effect.models.model_stability_gateway_key_provision import (
    EnumApiKeyEnsure,
    EnumTenantEnsure,
    ModelTenantEnsured,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    EnumProvisionKind,
    TenantIdentityPostgres,
)

_REFUSAL_DETAIL = {
    EnumProvisionKind.SLUG_TAKEN: "the slug belongs to a tenant with another owner",
    EnumProvisionKind.COMMAND_ID_REUSED: "the command id already produced a tenant for another slug",
    EnumProvisionKind.NO_DEFAULT_PLAN: "no default plan tier exists",
}


class GatewayIdentityPostgres:
    """``ProtocolGatewayIdentityStore`` over one ``TenantIdentityPostgres``."""

    def __init__(self, store: TenantIdentityPostgres) -> None:
        self._store = store

    async def ensure_tenant(self, *, tenant_slug: str, name: str) -> ModelTenantEnsured:
        record = await self._store.provision(
            ModelTenantProvisionCommand(
                command_id=uuid5(
                    NAMESPACE_URL, f"onex:gateway-key-tenant:{tenant_slug}"
                ),
                tenant_slug=tenant_slug,
                name=name,
            )
        )
        match record.kind:
            case EnumProvisionKind.CREATED:
                assert record.envelope is not None
                return ModelTenantEnsured(
                    kind=EnumTenantEnsure.CREATED,
                    tenant_id=UUID(record.envelope["payload"]["tenant"]["tenant_id"]),
                )
            case EnumProvisionKind.REPLAY:
                assert record.envelope is not None
                return ModelTenantEnsured(
                    kind=EnumTenantEnsure.EXISTS,
                    tenant_id=UUID(record.envelope["payload"]["tenant"]["tenant_id"]),
                )
            case EnumProvisionKind.ALREADY_EXISTS:
                assert record.tenant is not None
                return ModelTenantEnsured(
                    kind=EnumTenantEnsure.EXISTS, tenant_id=record.tenant.tenant_id
                )
            case _:
                return ModelTenantEnsured(
                    kind=EnumTenantEnsure.REFUSED,
                    detail=_REFUSAL_DETAIL[record.kind],
                )

    async def ensure_api_key(
        self, *, tenant_id: UUID, name: str, key_hash: str
    ) -> EnumApiKeyEnsure:
        kind = await self._store.ensure_api_key(
            tenant_id=tenant_id, name=name, key_hash=key_hash
        )
        return EnumApiKeyEnsure(kind.value)


__all__ = ["GatewayIdentityPostgres"]
