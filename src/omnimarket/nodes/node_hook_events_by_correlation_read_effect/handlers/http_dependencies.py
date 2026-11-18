# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract loading, the database adapter and the identity-resolver binding.

The database is the one the projection read binding overlay names, resolved
through the repository's existing projection binding surface; this module reads
no environment variable itself.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.edge_bearer_jwt_identity import (
    BearerJwtTenantIdentity,
    JwksSigningKeyProvider,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.model_edge_identity_overlay import ModelEdgeIdentityOverlay
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.models.model_hook_events_by_correlation import (
    ModelHookEventsHttpConfig,
)
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.protocols.protocol_hook_events_reader import (
    ProjectionAbsentError,
    ProjectionNotConfiguredError,
    ProjectionUnavailableError,
)
from omnimarket.projection.tenant_isolation import (
    TENANT_GUC,
    resolve_rls_read_tenant,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

logger = logging.getLogger(__name__)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

IDENTITY_OVERLAY_ID = "node_hook_events_by_correlation_read_effect"

# Projected columns are named explicitly (no SELECT *) so a schema addition
# upstream cannot silently widen what this route returns to a caller.
_SQL = """
SELECT
    correlation_id,
    event_id,
    run_id,
    event_type,
    source,
    tenant_id,
    occurred_at,
    captured_at,
    payload
FROM hook_events
WHERE tenant_id = $1
  AND correlation_id = $2
ORDER BY occurred_at DESC
LIMIT $3
"""


def load_contract() -> dict[str, Any]:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("hook-events contract must be a mapping")
    return raw


def load_http_config() -> ModelHookEventsHttpConfig:
    return ModelHookEventsHttpConfig.model_validate(load_contract()["http_route"])


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)


# The hosted edge binds the resolver in-process at startup. When nothing is
# bound explicitly, the resolver is built from this node's own overlay document
# and cached on success; an absent document is not cached so one that appears
# later is used without a restart.
_IDENTITY_RESOLVER: ProtocolHttpTenantIdentity | None = None
_FROM_OVERLAY: ProtocolHttpTenantIdentity | None = None


def _resolver_from_overlay() -> ProtocolHttpTenantIdentity | None:
    """Build the resolver from the node's overlay document, or ``None``.

    A missing document is not cached so a document that appears later is used
    without a restart; any other overlay error is logged once and not cached.
    """
    global _FROM_OVERLAY
    if _FROM_OVERLAY is not None:
        return _FROM_OVERLAY
    try:
        overlay = load_structured_overlay(IDENTITY_OVERLAY_ID, ModelEdgeIdentityOverlay)
    except NodeOverlayNotFoundError:
        return None
    except NodeOverlayError as exc:
        logger.warning(
            "identity overlay %s unavailable (%s)",
            IDENTITY_OVERLAY_ID,
            type(exc).__name__,
        )
        return None
    _FROM_OVERLAY = BearerJwtTenantIdentity(overlay, JwksSigningKeyProvider(overlay))
    return _FROM_OVERLAY


def bind_tenant_identity_resolver(resolver: ProtocolHttpTenantIdentity | None) -> None:
    """Bind (or with ``None`` unbind) the edge's caller-tenant resolver."""
    global _IDENTITY_RESOLVER, _FROM_OVERLAY
    _IDENTITY_RESOLVER = resolver
    _FROM_OVERLAY = None


def bound_tenant_identity_resolver() -> ProtocolHttpTenantIdentity | None:
    if _IDENTITY_RESOLVER is not None:
        return _IDENTITY_RESOLVER
    return _resolver_from_overlay()


def _decode_jsonb(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


class PostgresHookEventsReader:
    """Lazy pool over the projection read binding; reads one tenant's rows."""

    def __init__(self) -> None:
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> Any:
        async with self._lock:
            if self._pool is not None:
                return self._pool
            from omnimarket.projection.runner import (
                ProjectionReadBindingOverlayError,
                projection_read_binding_from_overlay_env,
            )

            try:
                binding = projection_read_binding_from_overlay_env()
            except ProjectionReadBindingOverlayError as exc:
                raise ProjectionNotConfiguredError(str(exc)) from exc
            if binding is None:
                raise ProjectionNotConfiguredError("no projection read binding")
            try:
                dsn = binding.resolve_database_url()
            except RuntimeError as exc:
                raise ProjectionNotConfiguredError("database ref unresolved") from exc
            import asyncpg

            try:
                self._pool = await asyncpg.create_pool(
                    dsn, min_size=1, max_size=4, command_timeout=5
                )
            except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
                raise ProjectionUnavailableError(type(exc).__name__) from exc
            return self._pool

    async def read(
        self, *, tenant_slug: str, correlation_id: str, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        import asyncpg

        # The caller's slug is the tenant. The resolver returns it unchanged for
        # a non-blank slug; for a blank one it would fall back to a configured or
        # house tenant, which this route must never read under, so a differing
        # answer refuses before any connection or statement.
        tenant = resolve_rls_read_tenant(tenant_slug, table="hook_events")
        if tenant != tenant_slug:
            raise ProjectionUnavailableError("tenant_unresolved")
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn, conn.transaction():
                # The RLS seam: the policy reads this setting. The explicit
                # predicate in the statement is the guard.
                await conn.execute(
                    "SELECT set_config($1, $2, true)", TENANT_GUC, tenant
                )
                records = await conn.fetch(_SQL, tenant, correlation_id, limit)
        except asyncpg.UndefinedTableError as exc:
            raise ProjectionAbsentError("hook_events") from exc
        except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
            logger.warning(
                "hook_events read failed: %s sqlstate=%s",
                type(exc).__name__,
                getattr(exc, "sqlstate", None),
            )
            raise ProjectionUnavailableError(type(exc).__name__) from exc
        rows: list[Mapping[str, Any]] = []
        for record in records:
            row = dict(record)
            row["payload"] = _decode_jsonb(row.get("payload"))
            rows.append(row)
        return rows

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


__all__: list[str] = [
    "CONTRACT_PATH",
    "IDENTITY_OVERLAY_ID",
    "PostgresHookEventsReader",
    "SystemClock",
    "bind_tenant_identity_resolver",
    "bound_tenant_identity_resolver",
    "load_contract",
    "load_http_config",
]
