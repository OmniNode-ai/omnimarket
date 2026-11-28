# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres access of the plan-assign node: one transaction over ``tenants``, ``plan_tiers``, ``tenant_plans`` and
``plan_entitlements`` of the ``omninode_cloud`` database.

The tenant row is locked, so two assigns of one tenant take turns. The previous active row is ended (``is_active`` false,
``ended_at`` set) and the new one inserted, so exactly one active row holds at any time and the history stays. The
entitlement rows are read in the same transaction, after the write. A driver failure is raised as
``TenantPlanStoreUnavailableError`` carrying the class name only: never a DSN, a statement or a driver message.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import asyncpg

from omnimarket.nodes.node_tenant_plan_assign_effect.models.model_tenant_plan_assign import (
    ModelPlanEntitlement,
)

_DRIVER_ERRORS = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
    TimeoutError,
)

_LOCK_TENANT = "SELECT tenant_id FROM tenants WHERE tenant_id = $1 FOR UPDATE"
_PLAN_EXISTS = "SELECT 1 FROM plan_tiers WHERE code = $1"
_ACTIVE_PLAN = (
    "SELECT plan_code FROM tenant_plans WHERE tenant_id = $1 AND is_active = true "
    "ORDER BY started_at DESC NULLS LAST LIMIT 1"
)
_END_ACTIVE = (
    "UPDATE tenant_plans SET is_active = false, ended_at = now() "
    "WHERE tenant_id = $1 AND is_active = true"
)
_INSERT_PLAN = "INSERT INTO tenant_plans (tenant_id, plan_code) VALUES ($1, $2)"
_ENTITLEMENTS = (
    "SELECT metric_key, window_key, limit_value, hard_cap, unit FROM plan_entitlements "
    "WHERE plan_code = $1 ORDER BY metric_key, window_key"
)


class TenantPlanStoreUnavailableError(Exception):
    """The identity database could not be reached or refused the statement; carries the class name only."""


class EnumPlanAssignKind(StrEnum):
    ASSIGNED = "assigned"
    ALREADY_ASSIGNED = "already_assigned"
    UNKNOWN_PLAN = "unknown_plan"
    UNKNOWN_TENANT = "unknown_tenant"


@dataclass(frozen=True)
class PlanAssignRecord:
    """What the transaction found and did, with the entitlements read back for the two success kinds."""

    kind: EnumPlanAssignKind
    previous_plan_code: str | None = None
    entitlements: tuple[ModelPlanEntitlement, ...] = ()


class TenantPlanPostgres:
    """Lazily created pool plus the assign transaction."""

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
                raise TenantPlanStoreUnavailableError(
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

    async def assign(self, *, tenant_id: UUID, plan_code: str) -> PlanAssignRecord:
        """Make ``plan_code`` the tenant's one active plan, or report why it was refused."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as connection, connection.transaction():
                if await connection.fetchrow(_LOCK_TENANT, tenant_id) is None:
                    return PlanAssignRecord(kind=EnumPlanAssignKind.UNKNOWN_TENANT)
                if await connection.fetchrow(_PLAN_EXISTS, plan_code) is None:
                    return PlanAssignRecord(kind=EnumPlanAssignKind.UNKNOWN_PLAN)
                current = await connection.fetchrow(_ACTIVE_PLAN, tenant_id)
                previous = None if current is None else str(current["plan_code"])
                kind = EnumPlanAssignKind.ALREADY_ASSIGNED
                if previous != plan_code:
                    await connection.execute(_END_ACTIVE, tenant_id)
                    await connection.execute(_INSERT_PLAN, tenant_id, plan_code)
                    kind = EnumPlanAssignKind.ASSIGNED
                rows = await connection.fetch(_ENTITLEMENTS, plan_code)
        except TenantPlanStoreUnavailableError:
            raise
        except _DRIVER_ERRORS as exc:
            raise TenantPlanStoreUnavailableError(type(exc).__name__) from None
        return PlanAssignRecord(
            kind=kind,
            previous_plan_code=previous,
            entitlements=tuple(
                ModelPlanEntitlement(
                    metric_key=r["metric_key"],
                    window_key=r["window_key"],
                    limit_value=r["limit_value"],
                    hard_cap=r["hard_cap"],
                    unit=r["unit"],
                )
                for r in rows
            ),
        )


__all__ = [
    "EnumPlanAssignKind",
    "PlanAssignRecord",
    "TenantPlanPostgres",
    "TenantPlanStoreUnavailableError",
]
