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

from omnimarket.local_deployment.tenant_identity import (
    ensure_install_identity_mirrored,
)
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
    TenantRegistryResolutionError,
    resolve_registry_tenant_uuid,
    sync_registry_tenant_uuid,
)

__all__ = ["require_tenant_mirrored"]


def require_tenant_mirrored(db: object, tenant_id: str) -> None:
    """Return when ``tenant_id`` resolves on ``db``'s mirror, else refuse.

    This install's own identity is mirrored first (see
    :func:`ensure_install_identity_mirrored`); a tenant handed in by a caller is
    never mirrored here. A target with no ``query``, or one that cannot be read
    or written (a locked or unwritable store), is a storage fault and not a
    mirror that lacks the tenant, so it does not refuse: the best-effort
    evidence write keeps its existing handling of it and the delegation
    response is never broken by the store.

    Raises:
        TenantRegistryResolutionError: the mirror (or the closed legacy mapping)
            holds no UUID for ``tenant_id``, or the two disagree.
    """
    if getattr(db, "query", None) is None:
        return
    try:
        ensure_install_identity_mirrored(db)
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
