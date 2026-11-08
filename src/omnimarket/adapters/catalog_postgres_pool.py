# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lazy read-only Postgres pool for the catalog nodes behind a runtime HTTP mount.

The address is resolved by logical name through the runtime secret resolver on the
first read, never from the process environment and never at import or app build.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import asyncpg
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.inference.secret_store_resolver import secret_store_from_config_path
from omnimarket.models.model_catalog_database_overlay import (
    ModelCatalogDatabaseOverlay,
)


class CatalogDatabaseNotConfiguredError(RuntimeError):
    """The lane supplies no secret resolver overlay or no value for the logical name."""


class CatalogPostgresPool:
    """Own one lazily created connection pool for read-only catalog queries."""

    def __init__(
        self,
        *,
        overlay_id: str,
        dsn_secret: str,
        max_size: int,
        query_timeout_seconds: float,
        secret_store: ProtocolSecretStore | None = None,
    ) -> None:
        self._overlay_id = overlay_id
        self._dsn_secret = dsn_secret
        self._max_size = max_size
        self._timeout = query_timeout_seconds
        self._secret_store = secret_store
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def _dsn(self) -> str:
        store = self._secret_store
        if store is None:
            overlay = load_structured_overlay(
                self._overlay_id, ModelCatalogDatabaseOverlay
            )
            store = secret_store_from_config_path(overlay.secret_resolver_config_path)
        value = await store.get_secret(self._dsn_secret)
        if not value:
            raise CatalogDatabaseNotConfiguredError(
                f"secret {self._dsn_secret} has no value"
            )
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

    async def fetch(self, sql: str) -> Sequence[Sequence[Any]]:
        pool = await self._get_pool()
        rows = await pool.fetch(sql)
        return [tuple(row.values()) for row in rows]

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


__all__: list[str] = ["CatalogDatabaseNotConfiguredError", "CatalogPostgresPool"]
