"""Asyncpg implementation of DatabaseAdapter."""

from __future__ import annotations

import logging
import os
from typing import Any

import asyncpg

from omnimarket.projection.tenant_isolation import TENANT_GUC, resolve_read_tenant

logger = logging.getLogger(__name__)

DB_URL_ENV = "OMNIDASH_ANALYTICS_DB_URL"


class AsyncpgAdapter:
    """DatabaseAdapter backed by asyncpg connection pool."""

    def __init__(
        self,
        dsn: str | None = None,
        min_size: int = 2,
        max_size: int = 10,
        command_timeout: float = 30,
    ) -> None:
        self._dsn = dsn or os.environ.get(DB_URL_ENV, "")
        self._min_size = min_size
        self._max_size = max_size
        self._command_timeout = command_timeout
        self._pool: asyncpg.Pool | None = None

    @property
    def dsn(self) -> str:
        """Return the DSN this adapter will dial (or is dialling)."""
        return self._dsn

    @property
    def is_connected(self) -> bool:
        """Whether a pool is open. A rebind is only honest before this is True."""
        return self._pool is not None

    def rebind(self, dsn: str) -> None:
        """Replace the DSN before the pool opens.

        OMN-16911. This adapter's default DSN is ``OMNIDASH_ANALYTICS_DB_URL``,
        the dashboard-facing login. A projection whose SQL names
        ``omninode_internal`` needs the workload identity the deployment
        topology binds for that schema instead, and only the runtime knows
        which that is -- it resolves the binding and proves its grants before
        wiring. This is the seam it hands the resolved DSN through.

        Refused once a pool exists: the open connections are already
        authenticated as the old role, so swapping the string underneath them
        would make ``self.dsn`` describe an identity the pool is not using.
        """
        candidate = dsn.strip()
        if not candidate:
            raise ValueError("projection database DSN must be non-empty")
        if self._pool is not None:
            raise RuntimeError(
                "cannot rebind the DSN of a connected adapter; its pool is "
                "already authenticated as the previous role"
            )
        self._dsn = candidate

    async def connect(self) -> None:
        if not self._dsn:
            raise RuntimeError(f"{DB_URL_ENV} not set and no DSN provided")
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            command_timeout=self._command_timeout,
        )
        logger.info(
            "asyncpg pool connected (min=%d, max=%d)", self._min_size, self._max_size
        )

    @property
    def pool(self) -> asyncpg.Pool:
        """Return the connected pool for canonical handlers that require it."""
        if self._pool is None:
            raise RuntimeError("call connect() first")
        return self._pool

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        assert self._pool is not None, "call connect() first"
        async with self._pool.acquire() as conn, conn.transaction():
            await self._set_tenant_context(conn, tenant)
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def execute_many(
        self,
        query: str,
        params_list: list[tuple[Any, ...]],
        *,
        tenant: str | None = None,
    ) -> None:
        assert self._pool is not None, "call connect() first"
        async with self._pool.acquire() as conn, conn.transaction():
            await self._set_tenant_context(conn, tenant)
            await conn.executemany(query, params_list)

    async def fetchval(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> Any:
        assert self._pool is not None, "call connect() first"
        async with self._pool.acquire() as conn, conn.transaction():
            await self._set_tenant_context(conn, tenant)
            return await conn.fetchval(query, *params)

    async def execute_in_transaction(
        self,
        queries: list[tuple[str, tuple[Any, ...]]],
        *,
        tenant: str | None = None,
    ) -> None:
        """Execute multiple queries in a single transaction."""
        assert self._pool is not None, "call connect() first"
        async with self._pool.acquire() as conn, conn.transaction():
            await self._set_tenant_context(conn, tenant)
            for query, params in queries:
                await conn.execute(query, *params)

    @staticmethod
    async def _set_tenant_context(conn: Any, tenant: str | None = None) -> None:
        """Set the RLS tenant GUC for pooled asyncpg operations.

        Tenant-scoped projection tables compare ``tenant_id`` against
        ``current_setting('app.tenant_id', true)``. ``SET LOCAL`` semantics only
        work inside a transaction, so every public query method opens the
        transaction first, sets the GUC with the parameterized ``set_config``
        form, and then runs the user SQL on the same connection.

        OMN-15919: ``tenant``, when supplied, is the value the CALLER already
        resolved for the write it is about to issue on this same connection --
        e.g. ``resolve_write_tenant(row.get("tenant_id"), table=...)`` against
        the exact row being upserted. This adapter must never re-derive a
        tenant of its own for that case: a second, independent resolver here
        (previously ``resolve_read_tenant(None)``, unconditionally, on every
        call) is exactly the two-divergent-paths shape that produced
        ``new row violates row-level security policy`` on every delegation
        write whose event/row tenant differed from the read-path default
        (OMN-15919 -- GUC stamped from a READ-path resolver while the row
        carried the real event tenant). ``tenant=None`` (the default) preserves
        the prior behavior for callers that are not yet tenant-aware -- most
        ``AsyncpgAdapter`` callers across this repo read/write single-tenant
        or house-tenant-only tables and have no per-call tenant to thread.
        """
        await conn.execute(
            "SELECT set_config($1, $2, true)",
            TENANT_GUC,
            tenant if tenant is not None else resolve_read_tenant(None),
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("asyncpg pool closed")
