# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres store of the flush: the unpublished rows of ``tenant_event_outbox`` in the identity database."""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg

from omnimarket.inference.secret_store_resolver import secret_store_from_config_path
from omnimarket.nodes.node_tenant_event_outbox_flush_effect.models.model_tenant_outbox_flush import (
    ModelOutboxRow,
)
from omnimarket.nodes.node_tenant_event_outbox_flush_effect.protocols.protocol_tenant_outbox_flush import (
    TenantOutboxStoreError,
)

_DRIVER_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)

PENDING_SQL = (
    "SELECT id, topic, envelope FROM tenant_event_outbox "
    "WHERE published_at IS NULL ORDER BY id LIMIT $1"
)
MARK_PUBLISHED_SQL = (
    "UPDATE tenant_event_outbox SET published_at = now() "
    "WHERE id = $1 AND published_at IS NULL"
)
MARK_FAILED_SQL = (
    "UPDATE tenant_event_outbox SET attempts = attempts + 1, last_error = $2 "
    "WHERE id = $1 AND published_at IS NULL"
)


def _unavailable(exc: BaseException) -> TenantOutboxStoreError:
    return TenantOutboxStoreError(type(exc).__name__)


class PostgresTenantOutboxStore:
    """ProtocolTenantOutboxStore over ``public.tenant_event_outbox``; the pool is created on first use."""

    def __init__(
        self,
        *,
        secret_resolver_config_path: str,
        dsn_secret: str,
        max_size: int,
        query_timeout_seconds: float,
        pool: asyncpg.Pool | None = None,
    ) -> None:
        self._config_path = secret_resolver_config_path
        self._dsn_secret_name = dsn_secret
        self._max_size = max_size
        self._timeout = query_timeout_seconds
        self._pool = pool
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        async with self._lock:
            if self._pool is None:
                store = secret_store_from_config_path(self._config_path)
                value = await store.get_secret(self._dsn_secret_name)
                if not value:
                    raise TenantOutboxStoreError("database secret has no value")
                self._pool = await asyncpg.create_pool(
                    dsn=value,
                    min_size=1,
                    max_size=self._max_size,
                    command_timeout=self._timeout,
                )
            return self._pool

    async def pending(self, limit: int) -> list[ModelOutboxRow]:
        try:
            pool = await self._get_pool()
            records = await pool.fetch(PENDING_SQL, limit)
        except TenantOutboxStoreError:
            raise
        except _DRIVER_ERRORS as exc:
            raise _unavailable(exc) from exc
        return [
            ModelOutboxRow(
                id=int(r["id"]), topic=str(r["topic"]), envelope=_as_dict(r["envelope"])
            )
            for r in records
        ]

    async def mark_published(self, row_id: int) -> None:
        await self._execute(MARK_PUBLISHED_SQL, row_id)

    async def mark_failed(self, row_id: int, error_class: str) -> None:
        await self._execute(MARK_FAILED_SQL, row_id, error_class)

    async def _execute(self, sql: str, *args: Any) -> None:
        try:
            pool = await self._get_pool()
            await pool.execute(sql, *args)
        except TenantOutboxStoreError:
            raise
        except _DRIVER_ERRORS as exc:
            raise _unavailable(exc) from exc

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        import json

        loaded = json.loads(value)
        return dict(loaded) if isinstance(loaded, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


__all__ = [
    "MARK_FAILED_SQL",
    "MARK_PUBLISHED_SQL",
    "PENDING_SQL",
    "PostgresTenantOutboxStore",
]
