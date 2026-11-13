# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lazy asyncpg pool for the workflow gateway's Postgres store.

Exemplar: src/omnimarket/models/model_catalog_postgres_pool.py. It differs by taking
parameters, by running writes, and by offering one transaction on one connection (the
quota read and the insert must be atomic under the tenant's advisory lock).

No connection and no secret read happens until the first call; the DSN is asked of the
runtime secret resolver by logical name, never read from the environment.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.inference.secret_store_resolver import secret_store_from_config_path
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_store import (
    WorkflowStoreUnavailableError,
)

logger = logging.getLogger(__name__)


class WorkflowTransaction:
    """Statements run on one connection inside one transaction."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return list(await self._connection.fetch(sql, *args))

    async def fetchrow(self, sql: str, *args: Any) -> Any | None:
        return await self._connection.fetchrow(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        status: str = await self._connection.execute(sql, *args)
        return status


def _unavailable(exc: BaseException) -> WorkflowStoreUnavailableError:
    # The class name only: never a DSN, a query or a driver message.
    return WorkflowStoreUnavailableError(type(exc).__name__)


class WorkflowPostgresPool:
    """One lazily created pool, closed with the mount."""

    def __init__(
        self,
        *,
        secret_resolver_config_path: str,
        dsn_secret: str,
        max_size: int,
        query_timeout_seconds: float,
        secret_store: ProtocolSecretStore | None = None,
    ) -> None:
        self._config_path = secret_resolver_config_path
        self._dsn_secret_name = dsn_secret
        self._max_size = max_size
        self._timeout = query_timeout_seconds
        self._secret_store = secret_store
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def _dsn(self) -> str:
        store = self._secret_store or secret_store_from_config_path(self._config_path)
        value = await store.get_secret(self._dsn_secret_name)
        if not value:
            raise WorkflowStoreUnavailableError("database secret has no value")
        return value

    async def _get_pool(self) -> asyncpg.Pool:
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    dsn=await self._dsn(),
                    min_size=1,
                    max_size=self._max_size,
                    command_timeout=self._timeout,
                )
            return self._pool

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        try:
            pool = await self._get_pool()
            return list(await pool.fetch(sql, *args))
        except WorkflowStoreUnavailableError:
            raise
        except Exception as exc:
            raise _unavailable(exc) from None

    async def fetchrow(self, sql: str, *args: Any) -> Any | None:
        try:
            pool = await self._get_pool()
            return await pool.fetchrow(sql, *args)
        except WorkflowStoreUnavailableError:
            raise
        except Exception as exc:
            raise _unavailable(exc) from None

    async def execute(self, sql: str, *args: Any) -> str:
        try:
            pool = await self._get_pool()
            status: str = await pool.execute(sql, *args)
            return status
        except WorkflowStoreUnavailableError:
            raise
        except Exception as exc:
            raise _unavailable(exc) from None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[WorkflowTransaction]:
        """One connection, one transaction; commits on a clean exit, rolls back on error."""
        try:
            pool = await self._get_pool()
        except WorkflowStoreUnavailableError:
            raise
        except Exception as exc:
            raise _unavailable(exc) from None
        async with pool.acquire() as connection, connection.transaction():
            yield WorkflowTransaction(connection)

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


__all__: list[str] = ["WorkflowPostgresPool", "WorkflowTransaction"]
