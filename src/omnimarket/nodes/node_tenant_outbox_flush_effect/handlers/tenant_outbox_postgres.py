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

_REQUIRED_COLUMNS = ("id", "envelope", "created_at", "published_at")
_TRACKING_NAMES = frozenset({"attempts", "last_error"})
_TABLE_COLUMNS = """
SELECT column_name FROM information_schema.columns
WHERE table_name = 'tenant_event_outbox' AND table_schema = ANY (current_schemas(false))
"""
_FETCH_UNPUBLISHED = """
SELECT id::text AS id, envelope, {attempts} AS attempts
FROM tenant_event_outbox
WHERE published_at IS NULL
ORDER BY created_at, id
LIMIT $1
"""
_MARK_PUBLISHED = """
UPDATE tenant_event_outbox
SET published_at = $2{clear_error}
WHERE id::text = $1 AND published_at IS NULL
"""
_RECORD_FAILURE = """
UPDATE tenant_event_outbox
SET attempts = attempts + 1, last_error = $2
WHERE id::text = $1 AND published_at IS NULL
"""


class TenantOutboxStoreUnavailableError(Exception):
    """The outbox database could not be reached or refused the statement; carries the class name only."""


class TenantOutboxSchemaError(TenantOutboxStoreUnavailableError):
    """The outbox table is absent or lacks a column the flush needs; the message names the table or the columns."""


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
        self._tracks_failures: bool | None = None

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

    async def _tracking(self, pool: asyncpg.Pool) -> bool:
        """Whether the table has the ``attempts`` and ``last_error`` columns; refuses a table the flush cannot use.

        The dev table ``node_tenant_provision_effect`` writes has neither tracking column; naming them there is an
        UndefinedColumnError on every statement. Without them a failure is reported in the flush result only and every row
        reads as zero attempts. A table that lacks a required column, or is absent, is refused here, by name, before any row
        statement is sent.
        """
        if self._tracks_failures is None:
            found = {r["column_name"] for r in await pool.fetch(_TABLE_COLUMNS)}
            if not found:
                raise TenantOutboxSchemaError(
                    "table tenant_event_outbox not found in the search path"
                )
            absent = [c for c in _REQUIRED_COLUMNS if c not in found]
            if absent:
                raise TenantOutboxSchemaError(
                    f"table tenant_event_outbox lacks column(s): {', '.join(absent)}"
                )
            self._tracks_failures = found >= _TRACKING_NAMES
        return self._tracks_failures

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def fetch_unpublished(self, limit: int) -> Sequence[ModelOutboxRow]:
        """Unpublished rows, oldest first; a row marked published is never returned."""
        try:
            pool = await self._get_pool()
            tracking = await self._tracking(pool)
            records = await pool.fetch(
                _FETCH_UNPUBLISHED.format(attempts="attempts" if tracking else "0"),
                limit,
            )
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
            tracking = await self._tracking(pool)
            await pool.execute(
                _MARK_PUBLISHED.format(
                    clear_error=", last_error = NULL" if tracking else ""
                ),
                row_id,
                published_at,
            )
        except TenantOutboxStoreUnavailableError:
            raise
        except _DRIVER_ERRORS as exc:
            raise _unavailable(exc) from None

    async def record_failure(self, row_id: str, error: str) -> None:
        try:
            pool = await self._get_pool()
            if await self._tracking(pool):
                await pool.execute(_RECORD_FAILURE, row_id, error)
        except TenantOutboxStoreUnavailableError:
            raise
        except _DRIVER_ERRORS as exc:
            raise _unavailable(exc) from None


__all__ = [
    "TenantOutboxPostgres",
    "TenantOutboxSchemaError",
    "TenantOutboxStoreUnavailableError",
]
