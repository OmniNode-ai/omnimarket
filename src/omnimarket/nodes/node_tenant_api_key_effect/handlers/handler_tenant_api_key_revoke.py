# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Revoke one credential row of one tenant. Idempotent: a second revoke answers already_revoked."""

from __future__ import annotations

import logging
from pathlib import Path

from omnimarket.nodes.node_tenant_api_key_effect.handlers.contract_settings import (
    CONTRACT_PATH,
    load_database_dsn_env,
)
from omnimarket.nodes.node_tenant_api_key_effect.handlers.store_tenant_api_key_postgres import (
    PostgresTenantApiKeyStore,
)
from omnimarket.nodes.node_tenant_api_key_effect.models import (
    EnumTenantApiKeyRevokeOutcome as Outcome,
)
from omnimarket.nodes.node_tenant_api_key_effect.models import (
    ModelTenantApiKeyRevokeRequest,
    ModelTenantApiKeyRevokeResult,
)
from omnimarket.nodes.node_tenant_api_key_effect.protocols import (
    ProtocolTenantApiKeyStore,
    TenantApiKeyStoreUnavailableError,
)

logger = logging.getLogger(__name__)


class HandlerTenantApiKeyRevoke:
    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        store: ProtocolTenantApiKeyStore | None = None,
    ) -> None:
        self._contract_path = contract_path or CONTRACT_PATH
        self._store = store

    def _store_port(self) -> ProtocolTenantApiKeyStore:
        if self._store is None:
            self._store = PostgresTenantApiKeyStore(
                dsn_env=load_database_dsn_env(self._contract_path)
            )
        return self._store

    async def handle(
        self, request: ModelTenantApiKeyRevokeRequest
    ) -> ModelTenantApiKeyRevokeResult:
        outcome = await self._decide(request)
        logger.info(
            "tenant api key revoke outcome=%s correlation_id=%s",
            outcome.value,
            request.correlation_id,
        )
        return ModelTenantApiKeyRevokeResult(
            correlation_id=request.correlation_id,
            tenant_id=request.tenant_id,
            credential_id=request.credential_id,
            outcome=outcome,
        )

    async def _decide(self, request: ModelTenantApiKeyRevokeRequest) -> Outcome:
        try:
            store = self._store_port()
            if await store.revoke_key(
                tenant_id=request.tenant_id, credential_id=request.credential_id
            ):
                return Outcome.REVOKED
            # The lookup is scoped by tenant and id: another tenant's row is not_found, never already_revoked.
            if await store.key_exists(
                tenant_id=request.tenant_id, credential_id=request.credential_id
            ):
                return Outcome.ALREADY_REVOKED
            return Outcome.NOT_FOUND
        except (TenantApiKeyStoreUnavailableError, OSError, ValueError, KeyError):
            return Outcome.STORE_UNAVAILABLE

    async def close(self) -> None:
        if self._store is not None:
            await self._store.close()


__all__: list[str] = ["HandlerTenantApiKeyRevoke"]
