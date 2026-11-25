# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The identity-table port on asyncpg: one lazy pool, the DSN read from the environment variable the contract names.

Nothing connects until the first call. A driver error becomes a typed error carrying the class name only, never a DSN, a
statement or a driver message.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from omnimarket.nodes.node_tenant_api_key_effect.models.model_tenant_api_key_row import (
    ModelTenantApiKeyRow,
)
from omnimarket.nodes.node_tenant_api_key_effect.models.model_tenant_state_reading import (
    ModelTenantStateReading,
)
from omnimarket.nodes.node_tenant_api_key_effect.protocols.protocol_tenant_api_key_store import (
    TenantApiKeyInsertConflictError,
    TenantApiKeyStoreSchemaOutdatedError,
    TenantApiKeyStoreUnavailableError,
)

_SQL_READ_BY_HASH = (
    "SELECT id, tenant_id, name, is_active, expires_at "
    "FROM tenant_api_keys WHERE key_hash = $1"
)
_SQL_TENANT_STATE = (
    "SELECT (SELECT provisioning_state FROM tenants WHERE tenant_id = $1), now()"
)
_SQL_INSERT = (
    "INSERT INTO tenant_api_keys (tenant_id, name, key_hash, expires_at) "
    "VALUES ($1, $2, $3, $4) RETURNING id, created_at"
)
_SQL_REVOKE = (
    "UPDATE tenant_api_keys SET is_active = false, revoked_at = now() "
    "WHERE id = $1 AND tenant_id = $2 AND revoked_at IS NULL RETURNING id"
)
_SQL_EXISTS = "SELECT 1 FROM tenant_api_keys WHERE id = $1 AND tenant_id = $2"

_POOL_MAX_SIZE = 4
_QUERY_TIMEOUT_SECONDS = 15.0


def _unavailable(exc: BaseException) -> TenantApiKeyStoreUnavailableError:
    if isinstance(exc, asyncpg.UndefinedColumnError):
        return TenantApiKeyStoreSchemaOutdatedError(type(exc).__name__)
    return TenantApiKeyStoreUnavailableError(type(exc).__name__)


class PostgresTenantApiKeyStore:
    def __init__(
        self, *, dsn_env: str, environ: Mapping[str, str] | None = None
    ) -> None:
        self._dsn_env = dsn_env
        self._environ = environ if environ is not None else os.environ
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        async with self._lock:
            if self._pool is None:
                dsn = self._environ.get(self._dsn_env, "")
                if not dsn:
                    raise TenantApiKeyStoreUnavailableError("DatabaseUrlNotSet")
                self._pool = await asyncpg.create_pool(
                    dsn=dsn,
                    min_size=1,
                    max_size=_POOL_MAX_SIZE,
                    command_timeout=_QUERY_TIMEOUT_SECONDS,
                )
            return self._pool

    async def _fetchrow(self, sql: str, *args: Any) -> Any | None:
        try:
            pool = await self._get_pool()
            return await pool.fetchrow(sql, *args)
        except TenantApiKeyStoreUnavailableError:
            raise
        except asyncpg.UniqueViolationError:
            raise TenantApiKeyInsertConflictError from None
        except Exception as exc:
            raise _unavailable(exc) from None

    async def read_by_key_hash(self, key_hash: str) -> ModelTenantApiKeyRow | None:
        row = await self._fetchrow(_SQL_READ_BY_HASH, key_hash)
        if row is None:
            return None
        return ModelTenantApiKeyRow(
            credential_id=row[0],
            tenant_id=row[1],
            name=row[2],
            is_active=row[3],
            expires_at=row[4],
        )

    async def read_tenant_state(self, tenant_id: UUID) -> ModelTenantStateReading:
        row = await self._fetchrow(_SQL_TENANT_STATE, tenant_id)
        if row is None:
            raise TenantApiKeyStoreUnavailableError("EmptyTenantStateReading")
        return ModelTenantStateReading(provisioning_state=row[0], database_now=row[1])

    async def insert_key(
        self, *, tenant_id: UUID, name: str, key_hash: str, expires_at: datetime | None
    ) -> tuple[UUID, datetime]:
        row = await self._fetchrow(_SQL_INSERT, tenant_id, name, key_hash, expires_at)
        if row is None:
            raise TenantApiKeyStoreUnavailableError("EmptyInsertReturning")
        return row[0], row[1]

    async def revoke_key(self, *, tenant_id: UUID, credential_id: UUID) -> bool:
        return await self._fetchrow(_SQL_REVOKE, credential_id, tenant_id) is not None

    async def key_exists(self, *, tenant_id: UUID, credential_id: UUID) -> bool:
        return await self._fetchrow(_SQL_EXISTS, credential_id, tenant_id) is not None

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


__all__: list[str] = ["PostgresTenantApiKeyStore"]
