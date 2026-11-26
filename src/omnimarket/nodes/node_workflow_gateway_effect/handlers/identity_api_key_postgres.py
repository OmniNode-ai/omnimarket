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
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_store import (
    WorkflowStoreUnavailableError,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

logger = logging.getLogger(__name__)

_SQL_RESOLVE_KEY = (
    "SELECT t.tenant_id, t.tenant_slug, k.id "
    "FROM tenant_api_keys k JOIN tenants t ON t.tenant_id = k.tenant_id "
    "WHERE k.key_hash = $1 AND k.is_active = true AND k.revoked_at IS NULL "
    "AND (k.expires_at IS NULL OR k.expires_at > now()) "
    "AND t.provisioning_state = 'active'"
)

# The probe names the relation the resolver queries, through the resolver's own pool, so a same-named table in another
# schema cannot satisfy it. LIMIT 0 reads no row.
_SQL_PROBE_EXPIRES_AT = "SELECT expires_at FROM tenant_api_keys LIMIT 0"
# The pool reports a driver error by its class name only.
_UNDEFINED_COLUMN = "UndefinedColumnError"
SCHEMA_OUTDATED_CODE = "api_key_store_schema_outdated"

_SQL_TOUCH_KEY = "UPDATE tenant_api_keys SET last_used_at = now() WHERE id = $1"


class ApiKeyStoreSchemaOutdatedError(WorkflowStoreUnavailableError):
    """The key table has no ``expires_at`` column; the gateway maps it to a 503 carrying ``code``."""

    code = SCHEMA_OUTDATED_CODE


class SchemaOutdatedApiKeyTenantIdentity:
    """The API-key path of a gateway whose key table predates ``expires_at``: every presented key is refused with a 503."""

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        raise ApiKeyStoreSchemaOutdatedError(SCHEMA_OUTDATED_CODE)


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
        if not value.isascii():
            return None
        # compare_digest raises TypeError on a non-ASCII str, so the comparison is on the UTF-8 bytes.
        value_bytes = value.encode("utf-8")
        prefix_bytes = self._prefix.encode("utf-8")
        if len(value_bytes) < len(prefix_bytes):
            return None
        if not hmac.compare_digest(value_bytes[: len(prefix_bytes)], prefix_bytes):
            return None
        key_hash = hashlib.sha256(value_bytes).hexdigest()
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


async def build_api_key_identity(
    *, pool: WorkflowPostgresPool, header: str, prefix: str
) -> ProtocolHttpTenantIdentity:
    """The API-key resolver, or the variant that answers 503 when the key table has no ``expires_at`` column.

    Absent column is a FAIL, logged once at ERROR; any other probe failure (the database is down) is raised for the
    caller to retry on the next request, never cached as a verdict on the schema.
    """
    try:
        await pool.fetch(_SQL_PROBE_EXPIRES_AT)
    except WorkflowStoreUnavailableError as exc:
        if str(exc) != _UNDEFINED_COLUMN:
            raise
        logger.error(
            "%s: tenant_api_keys has no expires_at column; every API-key request answers 503 until the schema is current",
            SCHEMA_OUTDATED_CODE,
        )
        return SchemaOutdatedApiKeyTenantIdentity()
    return PostgresApiKeyTenantIdentity(pool=pool, header=header, prefix=prefix)


__all__: list[str] = [
    "SCHEMA_OUTDATED_CODE",
    "ApiKeyStoreSchemaOutdatedError",
    "PostgresApiKeyTenantIdentity",
    "SchemaOutdatedApiKeyTenantIdentity",
    "build_api_key_identity",
]
