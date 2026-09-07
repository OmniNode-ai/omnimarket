# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Write-time tenant identity resolution against the runtime-populated registry.

OMN-16804. The delegation projection writer used to key ``tenant_id`` through
:data:`omnimarket.projection.tenant_isolation._LEGACY_TENANT_UUID_MAP` -- a
three-entry dict compiled into this source tree. The live registry
(``omninode_cloud.public.tenants``) held 39 tenants and gains more on every beta
signup, so every tenant outside those three raised
:class:`UnmappedTenantIdentityError` out of the writer, landed on the
malformed-event path, and was quarantined to the DLQ. A real, active,
externally-owned customer (provisioned 2026-08-26) was already in that set. Its
slug is deliberately NOT named here: this repo is PUBLIC, and OMN-17288 removed
that customer's slug and registry UUID from this tree. The claim this paragraph
makes -- that a live external tenant was in the unmapped set -- does not depend
on naming it.

The fix is the same one the operator green-lit for the apply path (OMN-16930,
verbatim ruling *"Hold + fix mechanism"*): resolve identity against a
**runtime-populated relation**, ``tenant_registry_mirror``, materialized in
``omnidash_analytics`` by ``node_projection_tenant_registry`` from
``onex.tenant.events``. One mechanism now serves both paths -- the migration
resolves at apply time by JOIN, the writer resolves at write time by lookup --
and neither inlines a literal map.

Three properties this module holds, in the order they are checked:

0. **The identity is keyed by its shape (OMN-16831).** The delegation wire
   carries the gateway's verified tenant UUID, so a UUID-shaped identity is
   confirmed against ``tenant_registry_mirror.tenant_uuid`` and never against
   ``tenant_slug`` or the slug-keyed legacy map. Only a non-UUID identity is
   treated as a slug. Keying ``tenant_slug`` for both shapes is what made every
   terminal delegation on onex-dev unresolvable while the mirror held the
   tenant under ``tenant_uuid`` the whole time.
1. **The registry wins.** A slug present in ``tenant_registry_mirror`` resolves
   to the UUID the registry recorded, always. That is the authenticated
   context's own identifier: ``onex-api`` wrote it in the same transaction that
   created the tenant (OMN-16027 durable outbox), and the projection carried it
   here. It is never caller-supplied.
2. **The legacy map is a closed historical fallback, never an authority.** It is
   consulted ONLY when the registry has no row, and ONLY for the three slugs
   this codebase wrote before the registry existed. This keeps the change
   monotonic: every tenant that resolved before still resolves, on a lane where
   ``node_projection_tenant_registry`` has not been deployed yet. It is not a
   default -- it cannot resolve a slug it does not already contain.
3. **Disagreement is a fault, not a preference.** If both sources answer and
   they answer differently, that is registry drift and this module raises rather
   than silently picking one. Two identifiers for one tenant is the failure this
   whole ticket exists to prevent.

Nothing here invents, derives, or defaults a tenant identity (OMN-16804 AC3). A
slug that neither source knows raises :class:`TenantRegistryResolutionError`,
whose message states that the projection has not caught up and names the node
responsible for catching it up -- the OMN-16930 diagnosis lesson, where a bare
``contains null values`` cost a week.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from omnimarket.projection.tenant_isolation import (
    _LEGACY_TENANT_UUID_MAP,
    UnmappedTenantIdentityError,
)

__all__ = [
    "TENANT_REGISTRY_MIRROR_TABLE",
    "TENANT_REGISTRY_PROJECTION_NODE",
    "ProtocolTenantRegistryReader",
    "TenantRegistryResolutionError",
    "async_registry_tenant_uuid",
    "parse_tenant_uuid",
    "resolve_registry_tenant_uuid",
    "resolve_registry_tenant_uuid_or_none",
    "sync_registry_tenant_uuid",
]

TENANT_REGISTRY_MIRROR_TABLE = "tenant_registry_mirror"
TENANT_REGISTRY_PROJECTION_NODE = "node_projection_tenant_registry"

_REGISTRY_SLUG_LOOKUP_SQL = (
    f"SELECT tenant_uuid FROM {TENANT_REGISTRY_MIRROR_TABLE} WHERE tenant_slug = $1"
)
_REGISTRY_UUID_LOOKUP_SQL = (
    f"SELECT tenant_uuid FROM {TENANT_REGISTRY_MIRROR_TABLE} WHERE tenant_uuid = $1"
)


def parse_tenant_uuid(tenant_identity: str | None) -> UUID | None:
    """Say which SHAPE a wire ``tenant_id`` carries: a UUID, or a slug.

    OMN-16831. This module was written believing the delegation wire carried a
    verified tenant *slug*, and every lookup in it keyed
    ``tenant_registry_mirror.tenant_slug``. The wire carries the gateway's
    verified *UUID*: ``ModelGatewayIdentity`` (omnibase_infra) types
    ``tenant_id: UUID`` beside a separate ``tenant_slug: str``, and only the
    UUID is ever put on an event -- ``handler_delegation_workflow
    ._resolve_tenant_id`` propagates it and nothing propagates the slug. So a
    canonical UUID was being matched against a TEXT slug column, which never
    matched, and every terminal delegation raised
    :class:`TenantRegistryResolutionError` and was quarantined. On onex-dev the
    mirror held the tenant all along: a count by ``tenant_uuid`` returned 1 for
    the same value a count by ``tenant_slug`` returned 0 for.

    Returning ``None`` means "this is not a UUID", i.e. treat it as a slug. It
    does not mean "absent" -- a blank identity is refused by the caller before
    this is reached.
    """
    if not isinstance(tenant_identity, str) or not tenant_identity.strip():
        return None
    try:
        return UUID(tenant_identity.strip())
    except ValueError:
        return None


class TenantRegistryResolutionError(ValueError):
    """A verified tenant slug resolves to no canonical UUID on this surface.

    Subclasses ``ValueError`` for the same reason
    :class:`UnmappedTenantIdentityError` does: the projection runner classifies
    it POISON and routes the event to quarantine rather than committing a row
    under an identity nobody can vouch for. Quarantine is the correct terminal
    state for an unattributable event -- what was wrong before was reaching it
    for ordinary, fully-provisioned customers.
    """


class ProtocolTenantRegistryReader(Protocol):
    """The one read this resolver needs, in whichever direction the caller runs.

    Deliberately narrow: an implementation hands back the recorded
    ``tenant_uuid`` for a slug, or ``None`` when the mirror holds no row for it.
    ``None`` means "the projection has not materialized this tenant", never
    "this tenant has no identity" -- the distinction the abort message carries.
    """

    def registry_tenant_uuid(self, tenant_slug: str) -> UUID | None:
        """Return the registry's UUID for ``tenant_slug``, or ``None``."""
        raise NotImplementedError


def _legacy_tenant_uuid(tenant_slug: str) -> UUID | None:
    """The closed pre-registry mapping, read without raising.

    Exists so the resolution order below can compare both answers. Callers on
    the write path must not reach into ``_LEGACY_TENANT_UUID_MAP`` themselves --
    the OMN-16804 AC4 guard fails closed on any write path that does.
    """
    return _LEGACY_TENANT_UUID_MAP.get(tenant_slug)


def resolve_registry_tenant_uuid(
    tenant_identity: str | None,
    *,
    registry_uuid: UUID | None,
) -> UUID:
    """Resolve a verified tenant slug to its canonical UUID, or refuse.

    ``registry_uuid`` is what ``tenant_registry_mirror`` holds for this slug
    (``None`` when it holds no row). Splitting the read out of this function
    keeps the decision itself synchronous, total and directly testable, while
    the async and sync write paths each perform their own lookup.

    Raises:
        TenantRegistryResolutionError: the identity is blank; or it is a UUID
            the mirror holds no row for; or it is a slug that neither the
            registry nor the closed legacy mapping knows; or the two disagree.
    """
    if not isinstance(tenant_identity, str) or not tenant_identity.strip():
        raise TenantRegistryResolutionError(
            "OMN-16804: refusing to resolve a blank tenant identity "
            f"({tenant_identity!r}). A projection row is attributed to the "
            "tenant the authenticated gateway verified; there is nothing to "
            "attribute this event to and no identity will be invented for it."
        )

    identity_uuid = parse_tenant_uuid(tenant_identity)
    if identity_uuid is not None:
        # OMN-16831: a UUID identity is confirmed against the mirror's
        # ``tenant_uuid`` column, never against ``tenant_slug`` and never
        # against the legacy map -- that map is keyed by slug and structurally
        # cannot answer for a UUID. The UUID is NOT taken on the caller's word:
        # ``registry_uuid`` is what the mirror returned for it, so an
        # unprovisioned or unmirrored identity still refuses rather than
        # attributing a row to an identifier nobody recorded (AC3).
        if registry_uuid is None:
            raise TenantRegistryResolutionError(
                f"OMN-16831: no row in {TENANT_REGISTRY_MIRROR_TABLE} whose "
                f"tenant_uuid is {tenant_identity!r}. The delegation wire "
                "carries the gateway's verified tenant UUID, and this resolver "
                "confirms it against the mirror rather than trusting it. Either "
                f"{TENANT_REGISTRY_PROJECTION_NODE} has not materialized this "
                "tenant from onex.tenant.events yet (historical tenants "
                "provisioned before that node went live are the known gap, "
                "OMN-17446), or this lane has not applied the mirror migration. "
                "No identity will be invented or defaulted for it."
            )
        return registry_uuid

    legacy_uuid = _legacy_tenant_uuid(tenant_identity)

    if registry_uuid is not None:
        if legacy_uuid is not None and legacy_uuid != registry_uuid:
            raise TenantRegistryResolutionError(
                f"OMN-16804: tenant registry drift for slug "
                f"{tenant_identity!r} -- "
                f"{TENANT_REGISTRY_MIRROR_TABLE} records {registry_uuid} while "
                f"the closed legacy mapping records {legacy_uuid}. Two "
                "identifiers for one tenant is exactly the split this resolver "
                "exists to prevent, so neither is used. Reconcile the registry "
                f"({TENANT_REGISTRY_PROJECTION_NODE}) against the legacy "
                "mapping before any further write to this surface."
            )
        return registry_uuid

    if legacy_uuid is not None:
        return legacy_uuid

    raise TenantRegistryResolutionError(
        f"OMN-16804: no canonical UUID for verified tenant slug "
        f"{tenant_identity!r}. {TENANT_REGISTRY_MIRROR_TABLE} holds no row "
        f"for it and it predates no legacy mapping. This means the tenant registry "
        f"projection has NOT CAUGHT UP -- it does not mean the tenant does not "
        f"exist. Check that {TENANT_REGISTRY_PROJECTION_NODE} is deployed and "
        "consuming onex.tenant.events, then replay this event. No identity "
        "will be invented or defaulted for it (OMN-16804 AC3)."
    )


def legacy_unmapped_error_is_still_reachable() -> type[UnmappedTenantIdentityError]:
    """Name the pre-OMN-16804 error type so its removal is a deliberate act.

    ``UnmappedTenantIdentityError`` still guards the *migration-derivation*
    reader in :mod:`omnimarket.projection.tenant_isolation`, which is correct --
    a conversion migration must refuse a slug it cannot express. Returning the
    type here keeps a single import-time reference from the write-path module,
    so a future change that deletes it cannot do so while believing the write
    path was its only caller.
    """
    return UnmappedTenantIdentityError


def _coerce_registry_uuid(value: object, *, tenant_identity: str) -> UUID | None:
    """Normalize whatever the adapter handed back into a UUID.

    asyncpg returns a real :class:`~uuid.UUID` for a ``uuid`` column; the
    in-memory and SQLite adapters used by the sync path return whatever the test
    or the local evidence store put in, which is a string. Both are accepted;
    anything else is a fault in the mirror, not an absent row, so it raises
    rather than degrading to ``None`` and quietly taking the legacy branch.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return UUID(value)
        except ValueError as exc:
            raise TenantRegistryResolutionError(
                f"OMN-16804: {TENANT_REGISTRY_MIRROR_TABLE} holds "
                f"{value!r} as the tenant_uuid for identity {tenant_identity!r}, "
                "which "
                "is not a UUID. The mirror is corrupt for this tenant; refusing "
                "to attribute a row to an unparseable identifier."
            ) from exc
    raise TenantRegistryResolutionError(
        f"OMN-16804: {TENANT_REGISTRY_MIRROR_TABLE} holds a tenant_uuid of "
        f"type {type(value).__name__} for identity {tenant_identity!r}. "
        "Expected a UUID "
        "or its string form."
    )


def _is_missing_relation(exc: BaseException) -> bool:
    """True when the failure is "the mirror table is not on this lane".

    A lane that has not applied the OMN-16930 migration has no
    ``tenant_registry_mirror``, and that is a DEPLOYMENT fact about the lane,
    not a fact about the tenant -- so it degrades to the closed legacy mapping
    rather than quarantining the event. Matched on the SQLSTATE asyncpg and
    psycopg both expose (``42P01`` undefined_table); any other database error
    propagates untouched, because a broken read is not an absent table.
    """
    return (
        getattr(exc, "sqlstate", None) == "42P01"
        or getattr(exc, "pgcode", None) == "42P01"
    )


async def async_registry_tenant_uuid(db: object, tenant_identity: str) -> UUID | None:
    """Read ``tenant_registry_mirror`` on the async (live Kafka) write path.

    OMN-16831: keys the column that matches the SHAPE of the identity on the
    wire. A UUID is matched against ``tenant_uuid``; anything else is matched
    against ``tenant_slug``. Keying ``tenant_slug`` unconditionally is what made
    every delegation terminal on onex-dev unresolvable.
    """
    fetchval = getattr(db, "fetchval", None)
    if fetchval is None:
        return None
    identity_uuid = parse_tenant_uuid(tenant_identity)
    if identity_uuid is not None:
        sql: str = _REGISTRY_UUID_LOOKUP_SQL
        param: object = identity_uuid
    else:
        sql = _REGISTRY_SLUG_LOOKUP_SQL
        param = tenant_identity
    try:
        raw = await fetchval(sql, param)
    except Exception as exc:
        if _is_missing_relation(exc):
            return None
        raise
    return _coerce_registry_uuid(raw, tenant_identity=tenant_identity)


def sync_registry_tenant_uuid(db: object, tenant_identity: str) -> UUID | None:
    """Read ``tenant_registry_mirror`` on the sync (CLI / batch) write path.

    OMN-16831: same shape-keyed predicate as the async twin above.
    """
    query = getattr(db, "query", None)
    if query is None:
        return None
    identity_uuid = parse_tenant_uuid(tenant_identity)
    predicate = (
        {"tenant_uuid": str(identity_uuid)}
        if identity_uuid is not None
        else {"tenant_slug": tenant_identity}
    )
    try:
        rows = query(TENANT_REGISTRY_MIRROR_TABLE, predicate)
    except Exception as exc:
        if _is_missing_relation(exc):
            return None
        raise
    if not rows:
        return None
    return _coerce_registry_uuid(
        rows[0].get("tenant_uuid"), tenant_identity=tenant_identity
    )


def resolve_registry_tenant_uuid_or_none(
    tenant_identity: str | None,
    *,
    registry_uuid: UUID | None,
) -> str | None:
    """Null-safe wrapper, preserving the OMN-14058 interim the operator accepted.

    A projection event that carries NO tenant at all is a different thing from
    one that carries a tenant nobody can resolve. The first is the accepted
    interim on this surface -- omitting the key lets the column DEFAULT apply on
    INSERT and leaves an already-known tenant untouched on UPDATE, and
    ``require_tenant_id`` is the gate that turns it into a refusal once
    ``ENFORCE_TENANT_ISOLATION`` is on. The second is the OMN-16804 defect and
    still raises.

    OMN-16804 deliberately does NOT change the absent-tenant case: widening the
    blast radius from "unresolvable tenant" to "no tenant" is a separate
    decision with its own ruling (OMN-16831 item 4 took it for
    ``generation_events`` alone, on the grounds that that relation was producing
    every row unattributed). Mirrors :func:`omnimarket.projection.
    tenant_isolation.resolve_tenant_uuid_or_none`'s contract exactly, so the
    only behavioural delta on this surface is WHERE a present slug resolves
    from.
    """
    if not tenant_identity or not tenant_identity.strip():
        return None
    return str(
        resolve_registry_tenant_uuid(tenant_identity, registry_uuid=registry_uuid)
    )
