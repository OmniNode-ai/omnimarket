"""Asyncpg implementation of DatabaseAdapter."""

from __future__ import annotations

import logging
import os
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

DB_URL_ENV = "OMNIDASH_ANALYTICS_DB_URL"


class AsyncpgAdapter:
    """DatabaseAdapter backed by asyncpg connection pool."""

    def __init__(
        self, dsn: str | None = None, min_size: int = 2, max_size: int = 10
    ) -> None:
        self._dsn = dsn or os.environ.get(DB_URL_ENV, "")
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if not self._dsn:
            raise RuntimeError(f"{DB_URL_ENV} not set and no DSN provided")
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            command_timeout=30,
        )
        logger.info(
            "asyncpg pool connected (min=%d, max=%d)", self._min_size, self._max_size
        )

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self._pool is not None, "call connect() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def execute_many(
        self, query: str, params_list: list[tuple[Any, ...]]
    ) -> None:
        assert self._pool is not None, "call connect() first"
        async with self._pool.acquire() as conn:
            await conn.executemany(query, params_list)

    async def fetchval(self, query: str, *params: Any) -> Any:
        assert self._pool is not None, "call connect() first"
        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *params)

    async def execute_in_transaction(
        self, queries: list[tuple[str, tuple[Any, ...]]]
    ) -> None:
        """Execute multiple queries in a single transaction."""
        assert self._pool is not None, "call connect() first"
        async with self._pool.acquire() as conn, conn.transaction():
            for query, params in queries:
                await conn.execute(query, *params)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("asyncpg pool closed")
