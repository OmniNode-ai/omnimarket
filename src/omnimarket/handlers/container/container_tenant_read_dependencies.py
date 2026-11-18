# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the tenant delegation reads' dependencies.

Each tenant-read node declares its protocols under ``handler_dependencies``. The
installed lane overlay binds each to an implementation:

* identity: the hosted edge binds the real resolver; the in-memory profile binds
  ``memory_http_tenant_identity``, which knows only the API keys it was seeded
  with and resolves nothing else. No implementation here accepts every caller.
* reader: ``memory_tenant_projection_reader`` (seeded rows) or
  ``postgres_tenant_projection_reader`` (analytics database, DSN resolved by
  logical secret name through the runtime secret resolver).

A protocol the overlay does not bind resolves to ``None``; the HTTP gate then
refuses every request instead of serving it unauthenticated.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from omnibase_core.models.nodes.handler_dependencies.model_overlay_bindings import (
    ModelOverlayBindings,
)

from omnimarket.handlers.container.container_overlay_dependencies import (
    installed_overlay,
)
from omnimarket.handlers.tenant_projection_read.analytics_postgres import (
    Psycopg2AnalyticsQueryExecutor,
    SecretResolverDsnSource,
    StaticDsnSource,
)
from omnimarket.handlers.tenant_projection_read.projection_reader import (
    AnalyticsTenantProjectionReader,
)
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.models.model_tenant_delegation_projection import (
    ModelDelegationRunRow,
    ModelTenantSavingsAggregate,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)
from omnimarket.protocols.protocol_logical_secret_resolver import (
    ProtocolLogicalSecretResolver,
)
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProjectionMissingError,
    ProtocolTenantDelegationProjectionReader,
)

IDENTITY_PROTOCOL = (
    f"{ProtocolHttpTenantIdentity.__module__}.{ProtocolHttpTenantIdentity.__qualname__}"
)
READER_PROTOCOL = (
    f"{ProtocolTenantDelegationProjectionReader.__module__}."
    f"{ProtocolTenantDelegationProjectionReader.__qualname__}"
)
API_KEY_HEADER = "x-api-key"

_secret_resolver: list[ProtocolLogicalSecretResolver] = []


def bind_secret_resolver(resolver: ProtocolLogicalSecretResolver | None) -> None:
    """Install the runtime secret resolver the postgres reader asks, or remove it."""
    _secret_resolver.clear()
    if resolver is not None:
        _secret_resolver.append(resolver)


class InMemoryHttpTenantIdentity:
    """Resolves only the API keys it was seeded with; every other caller is unknown."""

    def __init__(self, seeded: Mapping[str, ModelHttpTenantIdentity]) -> None:
        self._seeded = dict(seeded)

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        return self._seeded.get(headers.get(API_KEY_HEADER, ""))


class InMemoryTenantProjectionReader:
    """Seeded projection rows per tenant; records every call it receives."""

    def __init__(
        self,
        savings: Mapping[UUID, ModelTenantSavingsAggregate] | None = None,
        runs: Mapping[UUID, tuple[ModelDelegationRunRow, ...]] | None = None,
        *,
        fail_with: Exception | None = None,
    ) -> None:
        self._savings = dict(savings or {})
        self._runs = dict(runs or {})
        self._fail_with = fail_with
        self.calls: list[tuple[str, UUID]] = []

    async def read_savings(self, tenant_id: UUID) -> ModelTenantSavingsAggregate:
        self.calls.append(("read_savings", tenant_id))
        if self._fail_with is not None:
            raise self._fail_with
        return self._savings.get(tenant_id, ModelTenantSavingsAggregate())

    async def read_recent_delegations(
        self, tenant_id: UUID, limit: int
    ) -> tuple[ModelDelegationRunRow, ...]:
        self.calls.append(("read_recent_delegations", tenant_id))
        if self._fail_with is not None:
            raise self._fail_with
        rows = self._runs.get(tenant_id)
        if rows is None:
            raise ProjectionMissingError("no delegation rows seeded for the tenant")
        ordered = sorted(
            rows, key=lambda r: r.occurred_at or datetime.min, reverse=True
        )
        return tuple(ordered[:limit])


def memory_http_tenant_identity(config: Mapping[str, str]) -> object:
    """Binding config: API key -> ``<tenant uuid>,<slug>,<principal>``."""
    seeded: dict[str, ModelHttpTenantIdentity] = {}
    for key, value in config.items():
        tenant_id, slug, principal = value.split(",")
        seeded[key] = ModelHttpTenantIdentity(
            tenant_id=UUID(tenant_id), tenant_slug=slug, principal_id=principal
        )
    return InMemoryHttpTenantIdentity(seeded)


def memory_tenant_projection_reader(config: Mapping[str, str]) -> object:
    """Binding config: optional ``seed`` JSON of ``{tenant: {savings, runs}}``."""
    savings: dict[UUID, ModelTenantSavingsAggregate] = {}
    runs: dict[UUID, tuple[ModelDelegationRunRow, ...]] = {}
    seed: dict[str, Any] = json.loads(config.get("seed", "{}"))
    for tenant, block in seed.items():
        tenant_id = UUID(tenant)
        if "savings" in block:
            savings[tenant_id] = ModelTenantSavingsAggregate.model_validate(
                block["savings"]
            )
        if "runs" in block:
            runs[tenant_id] = tuple(
                ModelDelegationRunRow.model_validate(r) for r in block["runs"]
            )
    return InMemoryTenantProjectionReader(savings, runs)


def _postgres_factory(
    analytics_dsn_secret: str,
    uncounted_data_sources: Sequence[str],
) -> Callable[[Mapping[str, str]], object]:
    def build(config: Mapping[str, str]) -> object:
        dsn_source = (
            StaticDsnSource(config)
            if "dsn" in config
            else SecretResolverDsnSource(
                _secret_resolver[0] if _secret_resolver else None,
                analytics_dsn_secret,
            )
        )
        return AnalyticsTenantProjectionReader(
            Psycopg2AnalyticsQueryExecutor(dsn_source),
            uncounted_data_sources=uncounted_data_sources,
        )

    return build


def _build_one(
    overlay: ModelOverlayBindings | None,
    protocol: str,
    analytics_dsn_secret: str,
    uncounted_data_sources: Sequence[str] = (),
) -> object | None:
    if overlay is None:
        return None
    factories: dict[str, Callable[[Mapping[str, str]], object]] = {
        "memory_http_tenant_identity": memory_http_tenant_identity,
        "memory_tenant_projection_reader": memory_tenant_projection_reader,
        "postgres_tenant_projection_reader": _postgres_factory(
            analytics_dsn_secret, uncounted_data_sources
        ),
    }
    built = overlay.build([protocol], factories)
    if built.refusal_reason is not None:
        return None
    return built.dependencies.get(protocol)


def bound_identity(
    analytics_dsn_secret: str = "",
) -> ProtocolHttpTenantIdentity | None:
    """The identity resolver the installed overlay binds, or ``None`` when unbound."""
    bound = _build_one(installed_overlay(), IDENTITY_PROTOCOL, analytics_dsn_secret)
    return bound if isinstance(bound, ProtocolHttpTenantIdentity) else None


def bound_reader(
    analytics_dsn_secret: str,
    uncounted_data_sources: Sequence[str],
) -> ProtocolTenantDelegationProjectionReader | None:
    """The projection reader the installed overlay binds, or ``None`` when unbound."""
    bound = _build_one(
        installed_overlay(),
        READER_PROTOCOL,
        analytics_dsn_secret,
        uncounted_data_sources,
    )
    return (
        bound if isinstance(bound, ProtocolTenantDelegationProjectionReader) else None
    )
