# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one tenant's delegation and savings projections; bound by the lane overlay."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from omnimarket.models.model_tenant_delegation_projection import (
    ModelDelegationRunRow,
    ModelTenantSavingsAggregate,
)


class ProjectionMissingError(RuntimeError):
    """A projection table the read needs has not been provisioned yet."""


class ProjectionNotConfiguredError(RuntimeError):
    """The analytics source has no connection settings bound."""


class ProjectionUnavailableError(RuntimeError):
    """The analytics source is bound but unreachable or answered an error."""


@runtime_checkable
class ProtocolTenantDelegationProjectionReader(Protocol):
    """Tenant-scoped reads; every call is keyed by the canonical tenant UUID."""

    async def read_savings(self, tenant_id: UUID) -> ModelTenantSavingsAggregate: ...

    async def read_recent_delegations(
        self, tenant_id: UUID, limit: int
    ) -> tuple[ModelDelegationRunRow, ...]: ...


@runtime_checkable
class ProtocolAnalyticsQueryExecutor(Protocol):
    """Run one read-only parameterised query and return dict rows.

    ``tenant_id`` is set as the transaction-local row-level-security setting.
    Raises ``ProjectionMissingError``, ``ProjectionNotConfiguredError`` or
    ``ProjectionUnavailableError``.
    """

    async def run_query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class ProtocolAnalyticsDsnSource(Protocol):
    """The analytics connection string, resolved lazily; ``None`` when none is bound."""

    async def dsn(self) -> str | None: ...


__all__: list[str] = [
    "ProjectionMissingError",
    "ProjectionNotConfiguredError",
    "ProjectionUnavailableError",
    "ProtocolAnalyticsDsnSource",
    "ProtocolAnalyticsQueryExecutor",
    "ProtocolTenantDelegationProjectionReader",
]
