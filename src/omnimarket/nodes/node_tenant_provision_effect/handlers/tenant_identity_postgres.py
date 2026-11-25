# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres access of the tenant provision node: the identity tables of the ``omninode_cloud`` database.

One provision is one transaction over ``tenants``, ``tenant_plans``, ``plan_tiers`` and ``tenant_event_outbox``. The
store holds the SQL and the decision tree that depends on what the rows show; it knows nothing of the bus. The DSN is
asked of the secret store by the logical name the overlay gives (``tenant_identity.database.dsn``), and the pool is
created on first use, so a node that is never called never connects. A driver or connection failure is raised as
``TenantStoreUnavailableError`` carrying the class name only: never a DSN, a statement or a driver message.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg

from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_created_envelope import (
    TENANT_CREATED,
    TENANT_EVENTS_TOPIC,
    build_tenant_created_envelope,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    ModelTenantProvisionCommand,
)

_DRIVER_ERRORS = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
    TimeoutError,
)

_OUTBOX_BY_COMMAND = """
SELECT o.envelope, t.tenant_slug, t.owner_email
FROM tenant_event_outbox o
JOIN tenants t ON t.tenant_id = (o.envelope #>> '{payload,tenant,tenant_id}')::uuid
WHERE o.envelope ->> 'correlation_id' = $1
  AND o.envelope ->> 'operation' = $2
LIMIT 1
"""
_INSERT_TENANT = """
INSERT INTO tenants (tenant_slug, slug, name, owner_email, provisioning_state)
VALUES ($1, $1, $2, $3, 'active')
ON CONFLICT (tenant_slug) DO NOTHING
RETURNING tenant_id, tenant_slug, name, provisioning_state, created_at
"""
_TENANT_BY_SLUG = """
SELECT t.tenant_id, t.tenant_slug, t.name, t.provisioning_state, t.created_at, t.owner_email,
       (SELECT tp.plan_code FROM tenant_plans tp
         WHERE tp.tenant_id = t.tenant_id AND tp.is_active LIMIT 1) AS plan_code
FROM tenants t
WHERE t.tenant_slug = $1
"""
_TENANT_BY_ID = _TENANT_BY_SLUG.replace("t.tenant_slug = $1", "t.tenant_id = $1")
_DEFAULT_PLAN = "SELECT id, code FROM plan_tiers WHERE is_default ORDER BY code LIMIT 1"
_INSERT_PLAN = """
INSERT INTO tenant_plans (tenant_id, plan_code)
VALUES ($1, $2)
ON CONFLICT (tenant_id) WHERE is_active = true DO NOTHING
"""
_INSERT_OUTBOX = (
    "INSERT INTO tenant_event_outbox (topic, envelope) VALUES ($1, $2::jsonb)"
)
_INSERT_API_KEY = """
INSERT INTO tenant_api_keys (tenant_id, name, key_hash)
VALUES ($1, $2, $3)
ON CONFLICT DO NOTHING
RETURNING id
"""
_ACTIVE_KEY_HASH_BY_NAME = "SELECT key_hash FROM tenant_api_keys WHERE tenant_id = $1 AND name = $2 AND is_active"


class TenantStoreUnavailableError(Exception):
    """The identity database could not be reached or refused the statement; carries the class name only."""


class _NoDefaultPlanError(Exception):
    """Raised inside the transaction so that the tenant row is rolled back with the plan it could not get."""


class EnumProvisionKind(StrEnum):
    """What one provision transaction found and did."""

    CREATED = "created"
    REPLAY = "replay"
    ALREADY_EXISTS = "already_exists"
    SLUG_TAKEN = "slug_taken"
    COMMAND_ID_REUSED = "command_id_reused"
    NO_DEFAULT_PLAN = "no_default_plan"


class EnumApiKeyKind(StrEnum):
    """What one key insert found and did."""

    CREATED = "created"
    EXISTS = "exists"
    NAME_CONFLICT = "name_conflict"
    HASH_TAKEN = "hash_taken"


@dataclass(frozen=True)
class TenantRow:
    """One tenant as read: the six fields a read answers."""

    tenant_id: UUID
    tenant_slug: str
    name: str | None
    provisioning_state: str
    plan_code: str | None
    created_at: datetime


@dataclass(frozen=True)
class ProvisionRecord:
    """The transaction's result: ``envelope`` for created and replay, ``tenant`` for already-exists."""

    kind: EnumProvisionKind
    envelope: dict[str, Any] | None = None
    tenant: TenantRow | None = None


def _row(record: Any) -> TenantRow:
    return TenantRow(
        tenant_id=record["tenant_id"],
        tenant_slug=record["tenant_slug"],
        name=record["name"],
        provisioning_state=record["provisioning_state"],
        plan_code=record["plan_code"],
        created_at=record["created_at"],
    )


def _unavailable(exc: BaseException) -> TenantStoreUnavailableError:
    return TenantStoreUnavailableError(type(exc).__name__)


class TenantIdentityPostgres:
    """Lazily created pool plus the provision and read transactions."""

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
                raise TenantStoreUnavailableError("no database secret is configured")
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

    async def provision(self, command: ModelTenantProvisionCommand) -> ProvisionRecord:
        """One transaction: command-id check, tenant insert, then plan and outbox row, or the refusal it found."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as connection, connection.transaction():
                return await self._provision(connection, command)
        except _NoDefaultPlanError:
            return ProvisionRecord(kind=EnumProvisionKind.NO_DEFAULT_PLAN)
        except TenantStoreUnavailableError:
            raise
        except _DRIVER_ERRORS as exc:
            raise _unavailable(exc) from None

    async def _provision(
        self, connection: asyncpg.Connection, command: ModelTenantProvisionCommand
    ) -> ProvisionRecord:
        earlier = await self._by_command(connection, command)
        if earlier is not None:
            return earlier
        inserted = await connection.fetchrow(
            _INSERT_TENANT, command.tenant_slug, command.name, command.owner_email
        )
        if inserted is None:
            replay = await self._by_command(connection, command)
            if replay is not None:
                return replay
            existing = await connection.fetchrow(_TENANT_BY_SLUG, command.tenant_slug)
            if existing is None:
                raise TenantStoreUnavailableError("tenant row vanished during insert")
            if existing["owner_email"] == command.owner_email:
                return ProvisionRecord(
                    kind=EnumProvisionKind.ALREADY_EXISTS, tenant=_row(existing)
                )
            return ProvisionRecord(kind=EnumProvisionKind.SLUG_TAKEN)
        plan = await connection.fetchrow(_DEFAULT_PLAN)
        if plan is None:
            raise _NoDefaultPlanError
        await connection.execute(_INSERT_PLAN, inserted["tenant_id"], plan["code"])
        envelope = build_tenant_created_envelope(
            command_id=command.command_id,
            tenant_id=inserted["tenant_id"],
            tenant_slug=inserted["tenant_slug"],
            name=inserted["name"],
            status=inserted["provisioning_state"],
            created_at=inserted["created_at"],
            plan_code=plan["code"],
        )
        await connection.execute(
            _INSERT_OUTBOX, TENANT_EVENTS_TOPIC, json.dumps(envelope)
        )
        return ProvisionRecord(kind=EnumProvisionKind.CREATED, envelope=envelope)

    @staticmethod
    async def _by_command(
        connection: asyncpg.Connection, command: ModelTenantProvisionCommand
    ) -> ProvisionRecord | None:
        """The outbox row this command id already produced: a replay when slug and owner agree, else a reuse."""
        found = await connection.fetchrow(
            _OUTBOX_BY_COMMAND, str(command.command_id), TENANT_CREATED
        )
        if found is None:
            return None
        if (
            found["tenant_slug"] != command.tenant_slug
            or found["owner_email"] != command.owner_email
        ):
            return ProvisionRecord(kind=EnumProvisionKind.COMMAND_ID_REUSED)
        envelope: dict[str, Any] = json.loads(found["envelope"])
        return ProvisionRecord(kind=EnumProvisionKind.REPLAY, envelope=envelope)

    async def ensure_api_key(
        self, *, tenant_id: UUID, name: str, key_hash: str
    ) -> EnumApiKeyKind:
        """Insert one active key row for the tenant, or report what already holds the name or the hash (OMN-26586).

        ``ON CONFLICT DO NOTHING`` covers both unique constraints (``key_hash`` and the active ``(tenant_id, name)`` index);
        a conflict is then told apart by reading the active row under the name. Nothing is replaced or revoked.
        """
        try:
            pool = await self._get_pool()
            async with pool.acquire() as connection, connection.transaction():
                inserted = await connection.fetchrow(
                    _INSERT_API_KEY, tenant_id, name, key_hash
                )
                if inserted is not None:
                    return EnumApiKeyKind.CREATED
                held = await connection.fetchrow(
                    _ACTIVE_KEY_HASH_BY_NAME, tenant_id, name
                )
        except TenantStoreUnavailableError:
            raise
        except _DRIVER_ERRORS as exc:
            raise _unavailable(exc) from None
        if held is None:
            return EnumApiKeyKind.HASH_TAKEN
        if held["key_hash"] == key_hash:
            return EnumApiKeyKind.EXISTS
        return EnumApiKeyKind.NAME_CONFLICT

    async def read(
        self, *, tenant_id: UUID | None, tenant_slug: str | None
    ) -> TenantRow | None:
        """The tenant with this id or slug, or ``None``."""
        try:
            pool = await self._get_pool()
            if tenant_id is not None:
                record = await pool.fetchrow(_TENANT_BY_ID, tenant_id)
            else:
                record = await pool.fetchrow(_TENANT_BY_SLUG, tenant_slug)
        except TenantStoreUnavailableError:
            raise
        except _DRIVER_ERRORS as exc:
            raise _unavailable(exc) from None
        return None if record is None else _row(record)


__all__ = [
    "EnumApiKeyKind",
    "EnumProvisionKind",
    "ProvisionRecord",
    "TenantIdentityPostgres",
    "TenantRow",
    "TenantStoreUnavailableError",
]
