# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres store of the terminal writer: one transaction, lock, then a status-guarded update."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.inference.secret_store_resolver import secret_store_from_config_path
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalStoreOutcome,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_terminal_update import (
    ModelTerminalUpdate,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.protocols.protocol_gateway_terminal_store import (
    GatewayTerminalStoreError,
)

_TERMINAL = ("completed", "failed")

# Rows of every status, locked, so a concurrent opposite outcome waits for this transaction.
SQL_LOOKUP = (
    "SELECT workflow_id, status FROM public.gateway_workflows "
    "WHERE tenant_id = $1 AND correlation_id = $2 FOR UPDATE"
)

# A terminal that names no tenant (a publisher-malformed command, OMN-26566) joins on the
# correlation id alone; the update then writes with the tenant the one row carries.
SQL_LOOKUP_BY_CORRELATION = (
    "SELECT workflow_id, tenant_id, status FROM public.gateway_workflows "
    "WHERE correlation_id = $1 FOR UPDATE"
)

# First terminal wins: the status guard makes a replay or an opposite outcome change nothing.
SQL_APPLY = (
    "UPDATE public.gateway_workflows SET "
    "status = $3, terminal_model_used = $4, terminal_total_tokens = $5, terminal_latency_ms = $6, "
    "terminal_route = $7, terminal_provider = $8, terminal_credential_source = $9, "
    "result_content = $10, terminal_failure_reason = $11, terminal_failure_class = $12, "
    "terminal_failure_code = $13, terminal_rule_evaluations = $14::jsonb, "
    "completed_at = now(), updated_at = now() "
    "WHERE workflow_id = $1 AND tenant_id = $2 AND status NOT IN ('completed', 'failed') "
    "RETURNING workflow_id"
)


class GatewayTerminalPostgresPool:
    """One lazily created asyncpg pool; the DSN is asked of the secret resolver by logical name."""

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

    async def _get_pool(self) -> asyncpg.Pool:
        async with self._lock:
            if self._pool is None:
                store = self._secret_store or secret_store_from_config_path(
                    self._config_path
                )
                value = await store.get_secret(self._dsn_secret_name)
                if not value:
                    raise GatewayTerminalStoreError("database secret has no value")
                self._pool = await asyncpg.create_pool(
                    dsn=value,
                    min_size=1,
                    max_size=self._max_size,
                    command_timeout=self._timeout,
                )
            return self._pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            yield connection

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


class PostgresGatewayTerminalStore:
    """ProtocolGatewayTerminalStore over the gateway's public.gateway_workflows."""

    def __init__(self, pool: GatewayTerminalPostgresPool) -> None:
        self._pool = pool

    async def apply_terminal(
        self, update: ModelTerminalUpdate
    ) -> tuple[EnumGatewayTerminalStoreOutcome, UUID | None]:
        try:
            async with self._pool.transaction() as connection:
                if update.tenant_id is None:
                    rows: list[Any] = list(
                        await connection.fetch(
                            SQL_LOOKUP_BY_CORRELATION, update.correlation_id
                        )
                    )
                else:
                    rows = list(
                        await connection.fetch(
                            SQL_LOOKUP, update.tenant_id, update.correlation_id
                        )
                    )
                if not rows:
                    return EnumGatewayTerminalStoreOutcome.NO_ROW, None
                if len(rows) > 1:
                    return EnumGatewayTerminalStoreOutcome.AMBIGUOUS, None
                workflow_id: UUID = rows[0]["workflow_id"]
                tenant_id: UUID = (
                    rows[0]["tenant_id"]
                    if update.tenant_id is None
                    else update.tenant_id
                )
                if rows[0]["status"] in _TERMINAL:
                    return EnumGatewayTerminalStoreOutcome.ALREADY_TERMINAL, workflow_id
                changed = await connection.fetch(
                    SQL_APPLY,
                    workflow_id,
                    tenant_id,
                    update.status,
                    update.model_used,
                    update.total_tokens,
                    update.latency_ms,
                    update.route,
                    update.provider,
                    update.credential_source,
                    update.content,
                    update.failure_reason,
                    update.failure_class,
                    update.failure_code,
                    json.dumps(update.rule_evaluations),
                )
                if len(changed) != 1:
                    # Raised inside the transaction so it rolls back; never acknowledged.
                    raise GatewayTerminalStoreError(
                        "terminal update changed no single row"
                    )
                return EnumGatewayTerminalStoreOutcome.APPLIED, workflow_id
        except GatewayTerminalStoreError:
            raise
        except Exception as exc:
            # The class name only: never a DSN, a query or a driver message.
            raise GatewayTerminalStoreError(type(exc).__name__) from None


__all__: list[str] = [
    "SQL_APPLY",
    "SQL_LOOKUP",
    "SQL_LOOKUP_BY_CORRELATION",
    "GatewayTerminalPostgresPool",
    "PostgresGatewayTerminalStore",
]
