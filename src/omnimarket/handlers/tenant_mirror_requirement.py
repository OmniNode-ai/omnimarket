# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a run whose tenant the evidence target's mirror cannot resolve.

The evidence write resolves the tenant through ``tenant_registry_mirror``
(:func:`resolve_registry_tenant_uuid`) and, when it cannot, used to log a
WARNING and let the delegation complete with no ``delegation_events`` row.
:func:`require_tenant_mirrored` runs the SAME resolution against the SAME target
at the start of the run, so the refusal comes before any provider call and names
the tenant.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.local_deployment.tenant_identity import (
    _ensure_tables_on,
    ensure_install_identity_mirrored,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
    TenantRegistryResolutionError,
    resolve_registry_tenant_uuid,
    sync_registry_tenant_uuid,
)

__all__ = [
    "LANE_HOST_ENV",
    "declared_host_tenant",
    "ensure_declared_host_tenant_mirrored",
    "require_tenant_mirrored",
]

#: The variable a lane's runner sets to the name of its host's row of the lab host
#: table (the row whose ``tenant_id`` it also exports as ``ONEX_TENANT_ID``).
LANE_HOST_ENV = "ONEX_LANE_HOST"

#: What a declared-tenant mirror row records as its provenance.
_DECLARED_HOST_TENANT_SOURCE = "omn23371:lab-hosts-overlay-declared"


def declared_host_tenant(environ: Mapping[str, str] | None = None) -> UUID | None:
    """The tenant UUID the lab host table declares for THIS lane's host, else ``None``.

    The host is the row named by :data:`LANE_HOST_ENV`; the declaration is that row's
    ``tenant_id`` in the ``lab_hosts`` overlay (read as :func:`resolve_lab_hosts_table`
    reads it). ``None`` when the lane names no host, the table has no such row, the row
    declares no tenant, or the declaration is not a UUID. A table that cannot be read
    declares nothing: the caller then refuses as it did before this admission.
    """
    env = os.environ if environ is None else environ
    host_name = env.get(LANE_HOST_ENV, "").strip()
    if not host_name:
        return None
    try:
        table = resolve_lab_hosts_table(env)
    except NodeOverlayError:
        return None
    for row in table.hosts:
        if row.name == host_name and row.tenant_id:
            try:
                return UUID(row.tenant_id)
            except ValueError:
                return None
    return None


def ensure_declared_host_tenant_mirrored(
    db: object, tenant_id: str, *, environ: Mapping[str, str] | None = None
) -> None:
    """Record ``tenant_id`` in a local sqlite mirror when this host's row declares it.

    OMN-23371: a lab lane carries the tenant its host's row declares
    (``ONEX_TENANT_ID``), and the host's local mirror holds only the install's own
    minted identity, so the run was refused. Two bounds keep this a declaration and
    not a bypass: only the tenant the overlay declares for THIS host is written (any
    other tenant, including one declared for a different host, is left to refuse), and
    only a local sqlite evidence file is written (a deployed store is never seeded; that
    is ``node_projection_tenant_registry``'s job).
    """
    if not isinstance(db, SqliteDatabaseAdapter):
        return
    declared = declared_host_tenant(environ)
    if declared is None or str(declared) != tenant_id.strip().lower():
        return
    _ensure_tables_on(db)
    if db.query(TENANT_REGISTRY_MIRROR_TABLE, {"tenant_uuid": str(declared)}):
        return
    now = datetime.now(tz=UTC).isoformat()
    db.upsert(
        TENANT_REGISTRY_MIRROR_TABLE,
        "tenant_slug",
        {
            "tenant_slug": f"lab-{declared.hex[:12]}",
            "tenant_uuid": str(declared),
            "display_name": "lab host declared tenant",
            "status": "active",
            "registry_created_at": now,
            "observed_at": now,
            "source_event_id": _DECLARED_HOST_TENANT_SOURCE,
        },
    )


def require_tenant_mirrored(db: object, tenant_id: str) -> None:
    """Return when ``tenant_id`` resolves on ``db``'s mirror, else refuse.

    This install's own identity is mirrored first (see
    :func:`ensure_install_identity_mirrored`), then the tenant this host's row of
    the lab host table declares (see :func:`ensure_declared_host_tenant_mirrored`);
    any other tenant handed in by a caller is never mirrored here. A target with no
    ``query``, or one that cannot be read or written (a locked or unwritable store),
    is a storage fault and not a mirror that lacks the tenant, so it does not
    refuse: the best-effort evidence write keeps its existing handling of it and
    the delegation response is never broken by the store.

    Raises:
        TenantRegistryResolutionError: the mirror (or the closed legacy mapping)
            holds no UUID for ``tenant_id``, or the two disagree.
    """
    if getattr(db, "query", None) is None:
        return
    try:
        ensure_install_identity_mirrored(db)
        ensure_declared_host_tenant_mirrored(db, tenant_id)
        registry_uuid = sync_registry_tenant_uuid(db, tenant_id)
    except TenantRegistryResolutionError:
        raise
    except Exception:
        return
    try:
        resolve_registry_tenant_uuid(tenant_id, registry_uuid=registry_uuid)
    except TenantRegistryResolutionError as exc:
        raise TenantRegistryResolutionError(
            f"OMN-22221: refusing this delegation at its start: tenant "
            f"{tenant_id!r} is not a row of {TENANT_REGISTRY_MIRROR_TABLE} on "
            "the evidence target, so the run would complete with no "
            f"delegation_events row. Mirror the tenant first. Cause: {exc}"
        ) from exc
