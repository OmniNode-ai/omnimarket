# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only Postgres runner of the analytics projection database.

The connection string arrives through ``ProtocolAnalyticsDsnSource`` (resolved by
logical secret name), never from the process environment. Every query runs in one
transaction that first sets the transaction-local row-level-security setting.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import SecretStr

from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProjectionMissingError,
    ProjectionNotConfiguredError,
    ProjectionUnavailableError,
    ProtocolAnalyticsDsnSource,
)

logger = logging.getLogger(__name__)

RLS_SETTING = "app.tenant_id"
CONNECT_TIMEOUT_SECONDS = 5

# psycopg2 ships no type information; the driver is reached as an untyped module.
psycopg2: Any = importlib.import_module("psycopg2")
psycopg2_errors: Any = importlib.import_module("psycopg2.errors")
psycopg2_extras: Any = importlib.import_module("psycopg2.extras")
psycopg2_extras.register_uuid()


class ProtocolLogicalSecretResolver(Protocol):
    """The runtime secret resolver, asked for a secret by logical name."""

    async def get_secret_async(
        self, logical_name: str, required: bool = True
    ) -> SecretStr | None: ...


class SecretResolverDsnSource:
    """Resolve the analytics DSN by logical name; unbound resolver means no DSN."""

    def __init__(
        self, resolver: ProtocolLogicalSecretResolver | None, logical_name: str
    ) -> None:
        self._resolver = resolver
        self._logical_name = logical_name

    async def dsn(self) -> str | None:
        if self._resolver is None:
            return None
        value = await self._resolver.get_secret_async(
            self._logical_name, required=False
        )
        text = value.get_secret_value().strip() if value else ""
        return text or None


class StaticDsnSource:
    """A DSN carried by the binding itself (tests and proofs)."""

    def __init__(self, config: Mapping[str, str]) -> None:
        self._dsn = config.get("dsn", "").strip() or None

    async def dsn(self) -> str | None:
        return self._dsn


class Psycopg2AnalyticsQueryExecutor:
    """``ProtocolAnalyticsQueryExecutor`` over psycopg2, one connection per query."""

    def __init__(self, dsn_source: ProtocolAnalyticsDsnSource) -> None:
        self._dsn_source = dsn_source

    async def run_query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        dsn = await self._dsn_source.dsn()
        if not dsn:
            raise ProjectionNotConfiguredError("analytics source is not configured")
        return await asyncio.to_thread(
            self._run, dsn, sql, dict(params or {}), tenant_id
        )

    @staticmethod
    def _run(
        dsn: str, sql: str, params: dict[str, Any], tenant_id: str | None
    ) -> list[dict[str, Any]]:
        try:
            conn = psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS)
            try:
                with (
                    conn,
                    conn.cursor(cursor_factory=psycopg2_extras.RealDictCursor) as cur,
                ):
                    if tenant_id is not None:
                        cur.execute(
                            "SELECT set_config(%s, %s, true)", (RLS_SETTING, tenant_id)
                        )
                    cur.execute(sql, params)
                    return [dict(row) for row in cur.fetchall()]
            finally:
                conn.close()
        except psycopg2_errors.UndefinedTable as exc:
            raise ProjectionMissingError("projection table is absent") from exc
        except psycopg2.Error as exc:
            # Log what PostgreSQL said; raise what an HTTP client may see.
            logger.warning(
                "analytics DB error: %s sqlstate=%s",
                type(exc).__name__,
                getattr(exc, "pgcode", None),
            )
            raise ProjectionUnavailableError(
                f"analytics DB error ({type(exc).__name__})"
            ) from exc
