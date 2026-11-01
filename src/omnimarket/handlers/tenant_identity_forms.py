# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stored forms a credential read should match for one tenant.

Credential rows are stamped with the tenant name today and with the registry
UUID once the writer switches, so a reader that matches one exact string misses
the other form. :func:`sync_tenant_identity_forms` gives the readers both forms
from one unique, validated ``tenant_registry_mirror`` row, and nothing else.
"""

from __future__ import annotations

from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
    is_tenant_slug,
    parse_tenant_uuid,
)

__all__ = ["sync_tenant_identity_forms"]


def sync_tenant_identity_forms(db: object, tenant_identity: str) -> tuple[str, ...]:
    """Return the identity as given, plus its counterpart from the mirror.

    The counterpart comes from ONE ``tenant_registry_mirror`` row whose slug and
    UUID are both valid and that agrees with the identity given. Anything else --
    no reader, a failed read, no row, several rows, a malformed row, or a row that
    disagrees -- returns the identity alone, so the caller keeps its existing
    exact-string read and its existing error and fallback decisions.

    It is read-side only: none of the write path's refusals or legacy-map
    fallback (``resolve_registry_tenant_uuid``) apply here.
    """
    alone = (tenant_identity,)
    query = getattr(db, "query", None)
    if (
        query is None
        or not isinstance(tenant_identity, str)
        or not tenant_identity.strip()
    ):
        return alone
    identity_uuid = parse_tenant_uuid(tenant_identity)
    predicate = (
        {"tenant_uuid": str(identity_uuid)}
        if identity_uuid is not None
        else {"tenant_slug": tenant_identity}
    )
    try:
        rows = query(TENANT_REGISTRY_MIRROR_TABLE, predicate)
    except Exception:
        return alone
    if not isinstance(rows, list) or len(rows) != 1:
        return alone
    slug = rows[0].get("tenant_slug")
    row_uuid = parse_tenant_uuid(str(rows[0].get("tenant_uuid") or ""))
    if not isinstance(slug, str) or not is_tenant_slug(slug) or row_uuid is None:
        return alone
    if identity_uuid is not None:
        return (tenant_identity, slug) if row_uuid == identity_uuid else alone
    return (tenant_identity, str(row_uuid)) if slug == tenant_identity else alone
