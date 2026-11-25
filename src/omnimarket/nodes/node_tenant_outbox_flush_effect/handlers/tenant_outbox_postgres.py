# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres access of the outbox flush: the ``tenant_event_outbox`` table of the ``omninode_cloud`` database.

Implements ``ProtocolTenantOutboxStore``. The pool is created on first use from a DSN the secret store gives by the
logical name the overlay names, so a node that is never called never connects. A driver or connection failure is raised as
``TenantOutboxStoreUnavailableError`` carrying the class name only: never a DSN, a statement or a driver message.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

import asyncpg

from omnimarket.nodes.node_tenant_outbox_flush_effect.models.model_tenant_outbox_flush import (
    ModelOutboxRow,
)

_DRIVER_ERRORS = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
    TimeoutError,
)

_FETCH_UNPUBLISHED = """
SELECT id::text AS id, envelope, attempts
FROM tenant_event_outbox
WHERE published_at IS NULL
ORDER BY created_at, id
LIMIT $1
"""
_MARK_PUBLISHED = """
UPDATE tenant_event_outbox
SET published_at = $2, last_error = NULL
WHERE id::text = $1 AND published_at IS NULL
"""
_RECORD_FAILURE = """
UPDATE tenant_event_outbox
SET attempts = attempts + 1, last_error = $2
WHERE id::text = $1 AND published_at IS NULL
"""


class TenantOutboxStoreUnavailableError(Exception):
    """The outbox database could not be reached or refused the statement; carries the class name only."""


def _unavailable(exc: BaseException) -> TenantOutboxStoreUnavailableError:
    return TenantOutboxStoreUnavailableError(type(exc).__name__)


def _envelope(raw: Any) -> dict[str, Any]:
    decoded = json.loads(raw) if isinstance(raw, str | bytes) else raw
    if not isinstance(decoded, dict):
        raise TenantOutboxStoreUnavailableError("EnvelopeNotAnObject")
    return decoded


class TenantOutboxPostgres:
    """Lazily created pool plus the three statements the flush needs."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool | None = None,
        dsn_resolver: Callable[[], Awaitable[str]] | None = None,
        max_pool_size: int = 2,
        query_timeout_seconds: float = 10.0,
    ) -> None:
        self._pool = pool
        self._dsn_resolver = dsn_resolver
        self._max_pool_size = max_pool_size
        self._timeout = query_timeout_seconds

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            if self._dsn_resolver is None:
                raise TenantOutboxStoreUnavailableError(
                    "no database secret is configured"
                )
            self._pool = await asyncpg.create_pool(
                dsn=await self._dsn_resolver(),
                min_size=1,
                max_size=self._max_pool_size,
                command_timeout=self._timeout,
            )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def fetch_unpublished(self, limit: int) -> Sequence[ModelOutboxRow]:
        """Unpublished rows, oldest first; a row marked published is never returned."""
        try:
            pool = await self._get_pool()
            records = await pool.fetch(_FETCH_UNPUBLISHED, limit)
        except TenantOutboxStoreUnavailableError:
            raise
        except _DRIVER_ERRORS as exc:
            raise _unavailable(exc) from None
        return [
            ModelOutboxRow(
                row_id=str(record["id"]),
                envelope=_envelope(record["envelope"]),
                attempts=int(record["attempts"]),
            )
            for record in records
        ]

    async def mark_published(self, row_id: str, published_at: datetime) -> None:
        try:
            pool = await self._get_pool()
            await pool.execute(_MARK_PUBLISHED, row_id, published_at)
        except TenantOutboxStoreUnavailableError:
            raise
        except _DRIVER_ERRORS as exc:
            raise _unavailable(exc) from None

    async def record_failure(self, row_id: str, error: str) -> None:
        try:
            pool = await self._get_pool()
            await pool.execute(_RECORD_FAILURE, row_id, error)
        except TenantOutboxStoreUnavailableError:
            raise
        except _DRIVER_ERRORS as exc:
            raise _unavailable(exc) from None


__all__ = ["TenantOutboxPostgres", "TenantOutboxStoreUnavailableError"]
