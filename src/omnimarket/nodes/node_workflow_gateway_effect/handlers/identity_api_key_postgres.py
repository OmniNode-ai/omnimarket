# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""API-key tenant identity backed by Postgres."""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Mapping
from uuid import UUID

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_postgres_pool import (
    WorkflowPostgresPool,
)

logger = logging.getLogger(__name__)

_SQL_RESOLVE_KEY = (
    "SELECT t.tenant_id, t.tenant_slug, k.id "
    "FROM tenant_api_keys k JOIN tenants t ON t.tenant_id = k.tenant_id "
    "WHERE k.key_hash = $1 AND k.is_active = true AND k.revoked_at IS NULL"
)

_SQL_TOUCH_KEY = "UPDATE tenant_api_keys SET last_used_at = now() WHERE id = $1"


class PostgresApiKeyTenantIdentity:
    """Resolve a tenant from a hashed API key presented in the configured header."""

    def __init__(self, *, pool: WorkflowPostgresPool, header: str, prefix: str) -> None:
        self._pool = pool
        self._header = header
        self._prefix = prefix

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        value: str | None = None
        for key, val in headers.items():
            if key.lower() == self._header.lower():
                value = val
                break
        if value is None:
            return None
        if not value:
            return None
        if len(value) < len(self._prefix):
            return None
        prefix_slice = value[: len(self._prefix)]
        if not hmac.compare_digest(prefix_slice, self._prefix):
            return None
        key_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
        row = await self._pool.fetchrow(_SQL_RESOLVE_KEY, key_hash)
        if row is None:
            return None
        tenant_id: UUID = row[0]
        tenant_slug: str | None = row[1]
        if not tenant_slug:
            return None
        key_id: UUID = row[2]
        try:
            await self._pool.execute(_SQL_TOUCH_KEY, key_id)
        except Exception as exc:
            logger.warning(
                "best-effort last_used_at update failed (%s)", type(exc).__name__
            )
        return ModelHttpTenantIdentity(
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            principal_id=str(key_id),
        )


__all__: list[str] = ["PostgresApiKeyTenantIdentity"]
