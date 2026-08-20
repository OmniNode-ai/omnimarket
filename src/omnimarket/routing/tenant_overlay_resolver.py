# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-tenant delegation routing overlay resolver (OMN-15631 v1(a)).

Reads ``delegation_routing_tenant_overlay`` (migration 0001 under
``node_delegation_routing_reducer/migrations``) and resolves a tenant's
BACKEND BINDING override for one ``task_type`` -- the tenant-scoped
counterpart to :mod:`omnimarket.routing.roi_overlay`, following the identical
architecture for the identical reason:

    The routing reducer (``node_delegation_routing_reducer``) is a REDUCER --
    a pure, deterministic function with read-only *config* I/O only
    (``requires_network: false``). A live DB read inside ``delta`` would break
    fresh-process/live parity and golden-chain replay (OMN-12974). So the
    split is:

      * ``resolve_tenant_overlay_db`` / ``resolve_tenant_overlay`` (this
        module) -- the I/O boundary.
      * ``delta`` (the reducer) -- accepts the resolved overlay as a pure
        INPUT (``tenant_overlay=None`` by default, in which case behaviour is
        byte-identical to before this change -- AC4 tenant-zero equivalence).

AC6 seam (field-by-field, PR-body deliverable -- see migration 0001's header
for the full precedence writeup):

    Row (``delegation_routing_tenant_overlay``) -> :class:`ModelTenantRoutingOverlayBackend`
    is a 1:1 field mapping, no renaming, no derived fields:
        tenant_id, task_type, backend_id, endpoint_url, model_name  -- required
        secret_ref, timeout_ms, max_tokens                          -- optional

    Precedence: a matching (tenant_id, task_type) row WHOLESALE-REPLACES the
    platform-resolved backend for that pair -- it is never field-merged
    against the platform default (mixing a tenant's endpoint_url with the
    platform's model_name would silently address the wrong provider with the
    wrong model id). Routing STRUCTURE (tier order, escalation policy,
    pricing ceilings) is platform-fixed in v1(a) and is not represented here
    at all -- see ``delta``'s tenant-overlay short-circuit.

v1(a) explicit non-goals (design-feasibility comment 41f99997):
    - No DB-enforced cross-tenant denial (AC2) -- this table carries no RLS.
      Isolation here is an application-level ``tenant_id = ...`` filter only.
      AC2 is a follow-on ticket blocked on OMN-14894/OMN-15356.
    - Platform default remains the existing repo-YAML resolution
      (``_load_bifrost_endpoints``) -- NOT yet rows in ``platform_catalog``.
      A tenant with no overlay row resolves the unchanged platform-default
      path (AC3's "no overlay -> platform default" half); the "provably
      vendor-neutral platform_catalog row" half of AC3 requires that separate
      migration and is out of v1(a)'s scope.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG

logger = logging.getLogger(__name__)

#: Same DSN the sibling ROI overlay reads (omnimarket.routing.roi_overlay) --
#: one canonical Postgres this package talks to for routing-time reads.
_ENV_TENANT_OVERLAY_DSN = "OMNIDASH_ANALYTICS_DB_URL"

#: Table created by migration 0001 (node_delegation_routing_reducer/migrations).
TENANT_OVERLAY_TABLE = "delegation_routing_tenant_overlay"


class ModelTenantRoutingOverlayBackend(BaseModel):
    """Resolved tenant-overlay backend binding for one (tenant_id, task_type).

    A 1:1 field mapping of one ``delegation_routing_tenant_overlay`` row (see
    module docstring for the AC6 seam). Frozen -- a routing decision snapshot,
    never mutated after resolution.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    task_type: str
    backend_id: str
    endpoint_url: str
    model_name: str
    secret_ref: str | None = None
    timeout_ms: int | None = None
    max_tokens: int | None = None


@runtime_checkable
class ProtocolTenantOverlayReader(Protocol):
    """Structural protocol satisfied by ``PostgresReadDatabaseAdapter`` and fakes."""

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]: ...


def resolve_tenant_overlay_db() -> ProtocolTenantOverlayReader | None:
    """Resolve a read-only adapter for ``delegation_routing_tenant_overlay``.

    Gated on the same ``OMNIDASH_ANALYTICS_DB_URL`` DSN
    :func:`omnimarket.routing.roi_overlay.resolve_context_roi_db` reads.
    Returns ``None`` when the DSN is unset or construction fails, so the
    caller falls through to the unchanged platform-default resolution path
    (fail-OPEN for outage — this mirrors the ROI overlay's posture: a
    tenant-overlay read failure must never break a delegation that the
    platform default would have served).

    ``delegation_routing_tenant_overlay`` carries NO RLS in v1(a) (migration
    0001) — reusing ``PostgresReadDatabaseAdapter`` here is for its `query()`
    convenience only; the GUC it sets is inert against this specific table
    because no policy on it references ``app.tenant_id``. Do not read this as
    RLS coverage — AC2 (DB-enforced denial) is explicitly deferred.
    """
    dsn = os.environ.get(_ENV_TENANT_OVERLAY_DSN, "").strip()
    if not dsn:
        return None
    try:
        from omnimarket.projection.postgres_read_database import (
            PostgresReadDatabaseAdapter,
        )

        return PostgresReadDatabaseAdapter(dsn, tenant_id=HOUSE_TENANT_SLUG)
    except Exception:
        logger.warning(
            "resolve_tenant_overlay_db failed to construct a read adapter from "
            "%s; tenant overlay resolution disabled (platform default only)",
            _ENV_TENANT_OVERLAY_DSN,
            exc_info=True,
        )
        return None


def resolve_tenant_overlay(
    db: ProtocolTenantOverlayReader | None,
    *,
    tenant_id: str | None,
    task_type: str,
) -> ModelTenantRoutingOverlayBackend | None:
    """Resolve a tenant's routing overlay for one ``task_type``, or ``None``.

    Pure I/O-boundary function (mirrors ``resolve_roi_overlay``): the caller
    (the routing handler's bus/local dispatch boundary) reads this ONCE per
    request and threads the RESULT into ``delta()`` as a pure input --
    ``delta()`` itself never touches the database (REDUCER_GENERIC purity).

    Tenant-zero fast path (AC4): ``tenant_id`` unset or equal to
    :data:`HOUSE_TENANT_SLUG` returns ``None`` WITHOUT issuing a query, so
    tenant-zero's resolution never touches this table and is byte-identical
    to the pre-OMN-15631 behaviour.

    Fail-open on read errors, an absent DSN (``db is None``), or no matching
    row -- all return ``None`` and the caller falls through to the unchanged
    platform-default resolution (AC3's "no overlay -> platform default").
    A row that exists but fails to parse into
    :class:`ModelTenantRoutingOverlayBackend` (a required column is somehow
    NULL despite the ``NOT NULL`` constraint, or of the wrong type) is NOT
    absorbed into that fail-open posture -- it raises, because a malformed
    stored override is a data-integrity defect, not a telemetry outage, and
    routing tenant-zero's endpoint to a mis-shapen tenant row would be worse
    than failing loudly.
    """
    if not tenant_id or tenant_id == HOUSE_TENANT_SLUG:
        return None
    if db is None:
        return None
    try:
        rows = db.query(
            TENANT_OVERLAY_TABLE,
            filters={"tenant_id": tenant_id, "task_type": task_type},
        )
    except Exception:
        logger.warning(
            "tenant overlay read failed for tenant_id=%s task_type=%s; "
            "falling through to platform default",
            tenant_id,
            task_type,
            exc_info=True,
        )
        return None
    if not rows:
        return None
    row = rows[0]
    return ModelTenantRoutingOverlayBackend(
        tenant_id=str(row["tenant_id"]),
        task_type=str(row["task_type"]),
        backend_id=str(row["backend_id"]),
        endpoint_url=str(row["endpoint_url"]),
        model_name=str(row["model_name"]),
        secret_ref=_optional_str(row.get("secret_ref")),
        timeout_ms=_optional_int(row.get("timeout_ms")),
        max_tokens=_optional_int(row.get("max_tokens")),
    )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"expected an int-like value, got bool: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise ValueError(
        f"expected an int-like value, got {type(value).__name__}: {value!r}"
    )


__all__: list[str] = [
    "TENANT_OVERLAY_TABLE",
    "ModelTenantRoutingOverlayBackend",
    "ProtocolTenantOverlayReader",
    "resolve_tenant_overlay",
    "resolve_tenant_overlay_db",
]
