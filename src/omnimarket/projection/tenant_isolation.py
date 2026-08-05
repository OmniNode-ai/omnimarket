# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail-closed tenant-isolation guard for projection writers (OMN-14898).

Ground truth (superseding OMN-14898's own Phase-2 assumption): the envelope-side
tenant stamp is already canonical (``omnibase_infra.shared.tenant_stamp``,
OMN-14208) and the DB-side RLS enforcement already landed (migration 0023,
OMN-14894 tranche 1). The remaining writer-boundary gap is that
``HandlerProjectionDelegation``/``materialize_budget_state`` silently OMIT the
key when the source event carries no tenant identity (OMN-14058, OPERATOR-
ACCEPTED INTERIM) -- Postgres' ``DEFAULT 'omninode'``/``DEFAULT_TENANT`` then
absorbs the write instead of refusing it
(``feedback_optional_input_means_the_check_does_not_exist``).

Why this guard is OPT-IN (``ENFORCE_TENANT_ISOLATION``, default ``False``),
not an unconditional reject:

  * ``Settings.onex_tenant_id`` defaults to ``""`` TODAY across every lane --
    the OMN-14058 single-tenant interim is the deliberate, operator-accepted
    default state of the fleet, not a bug. An unconditional reject would take
    down the entire delegation/budget-state write path in every lane that has
    not yet configured a tenant, which is every lane before the Aug 10
    multi-tenancy cutover.
  * OMN-14894's own migration 0023 documents that writers connect as the
    ``postgres`` SUPERUSER on compose lanes and bypass RLS entirely regardless
    of this guard -- so today, this check is defense-in-depth, not the
    isolation boundary. The isolation boundary is the RLS policy plus the
    non-superuser ``app_dashboard``-style writer role (OMN-14899, Daniyal-
    owned live role/credential wiring), not this application-level guard.
  * OMN-14362 (per-tenant broker ACLs) is the trust anchor for the stamp
    itself -- without it, an enforced tenant_id is merely self-asserted, not
    authenticated. Flipping enforcement on before OMN-14362 lands would make a
    forgeable field mandatory without making it trustworthy.

Flip ``ENFORCE_TENANT_ISOLATION=true`` only in a lane that has verified (a)
the envelope stamp is authenticated (OMN-14362) and (b) the writer connects as
a non-superuser role so RLS actually binds (OMN-14899) -- otherwise this
guard rejects real single-tenant traffic for no isolation benefit.
"""

from __future__ import annotations

from uuid import UUID

from omnimarket.config.settings import get_settings


class UnmappedTenantIdentityError(ValueError):
    """Raised when a legacy tenant value has no canonical UUID mapping (OMN-15356).

    The conversion migration for every classified-TENANT relation calls
    :func:`resolve_tenant_uuid` inside the same transaction as the column
    type change. This exception aborts that transaction -- Postgres rolls
    back the whole ``ALTER COLUMN ... TYPE`` on any unmapped row, so a
    partially-converted column, a silently invented UUID, or a sentinel
    value can never be the result. "No sentinel survives" (this ticket's own
    acceptance criterion) is enforced by this being a raise, not a return.
    """


class TenantRequiredError(ValueError):
    """Raised when a projection write is refused for missing ``tenant_id``.

    OMN-14898: with ``ENFORCE_TENANT_ISOLATION=true``, a writer that resolves
    no tenant identity refuses the write rather than falling through to the
    shared ``'omninode'``/``'default'`` column default. The caller must raise
    this BEFORE any ``db.upsert()`` call so a refused write produces zero rows
    -- never a partially-written or default-tenant row.
    """


# OMN-15306: the GUC the RLS policies compare ``tenant_id`` against (migration
# 0023 and siblings). Shared with the onex-api and omnidash reader seams -- a
# cross-repo contract, not a local choice.
TENANT_GUC = "app.tenant_id"

# ---------------------------------------------------------------------------
# The house tenant (OPERATOR RULING 2026-08-02).
#
# OmniNode is a first-class TENANT, not an absence of one. Every
# workload-attributable row belongs to some tenant; rows produced by the
# platform's own workloads belong to the house tenant. Only
# attribution-meaningless infrastructure state (migration bookkeeping, service
# registry, orchestration state, deployment evidence) stays in
# ``omninode_internal``.
#
# ONE identity, TWO representations, deliberately -- do not add a third:
#
#   HOUSE_TENANT_SLUG  the wire/column form most relations still carry. Every
#                      landed DEFAULT was this exact string (delegation
#                      0022/0025, savings 080, registration 0002,
#                      inference-response 0002, omnidash 0001_tenant_rls).
#                      Introducing a UUID column alongside a TEXT one would
#                      fork the identity inside one database -- OMN-15356
#                      converts each relation's column in place instead
#                      (capability_scores migration 0003 is the first landed
#                      conversion; the remaining classified-TENANT relations
#                      follow the identical pattern). :func:`resolve_tenant_uuid`
#                      is the single Python implementation of that mapping;
#                      ``house_tenant_map_slug_to_uuid`` in each conversion
#                      migration is its SQL mirror.
#
#   HOUSE_TENANT_UUID  the canonical immutable identifier ADR-0027 requires and
#                      the value every OMN-15356 conversion resolves the slug
#                      to. Pinned here so the conversion has a fixed,
#                      reviewable target instead of minting one at migration
#                      time.
#
# The UUID is a UUIDv5 -- derived, not random, so it is reproducible from first
# principles and independently checkable:
#
#     uuid.uuid5(uuid.NAMESPACE_DNS, "house-tenant.omninode.ai")
#     -> 820272f9-4aaf-5add-a2df-0af942852ab2
#
# ``tests/unit/projection/test_house_tenant_identity.py`` re-derives it, so a
# hand-edit of the literal fails CI.
# ---------------------------------------------------------------------------
HOUSE_TENANT_SLUG = "omninode"
HOUSE_TENANT_UUID = UUID("820272f9-4aaf-5add-a2df-0af942852ab2")

# Pre-ruling name for HOUSE_TENANT_SLUG, retained because the string is the
# same value and callers/comments across this repo already reference it. It is
# no longer accurate as a description: the value is not an "interim default
# standing in for a missing tenant", it is a real tenant's identifier.
INTERIM_DEFAULT_TENANT = HOUSE_TENANT_SLUG


# ---------------------------------------------------------------------------
# OMN-15356: legacy TEXT tenant identity -> canonical UUID.
#
# A closed, explicit mapping table -- NOT a hash/uuid5 re-derivation of
# whatever string shows up. The only legacy value any landed migration or
# writer has ever produced is HOUSE_TENANT_SLUG (every DEFAULT, every
# backfill, every handler constant agrees -- see
# ``test_house_tenant_identity.py``). Re-deriving a UUID from an arbitrary
# input string would let any unreviewed value silently mint a new tenant
# identity with no registry entry and no RLS policy; this table can only ever
# resolve values this codebase already knows about, and everything else is
# refused. Extend this dict, never fall through to a computed default, when a
# second legacy value is discovered.
# ---------------------------------------------------------------------------
_LEGACY_TENANT_UUID_MAP: dict[str, UUID] = {
    HOUSE_TENANT_SLUG: HOUSE_TENANT_UUID,
}


def resolve_tenant_uuid(tenant_value: str | None) -> UUID:
    """Map a legacy TEXT ``tenant_id`` value to its canonical UUID (OMN-15356).

    Total over the closed set of values this codebase has ever written, and
    over nothing else: exact, case-sensitive, whitespace-sensitive string
    match against :data:`_LEGACY_TENANT_UUID_MAP`. Anything not an exact key
    -- ``None``, blank, mis-cased, padded, or a value belonging to a tenant
    this repo has no record of -- raises :class:`UnmappedTenantIdentityError`
    rather than guessing. This is the single implementation the
    ``ALTER COLUMN ... TYPE UUID USING`` conversion for every classified
    TENANT relation is derived from (SQL mirror:
    ``house_tenant_map_slug_to_uuid`` in migration 0003 of
    ``node_canary_score_reducer``, and every relation that follows it).
    """
    if isinstance(tenant_value, str):
        mapped = _LEGACY_TENANT_UUID_MAP.get(tenant_value)
        if mapped is not None:
            return mapped
    raise UnmappedTenantIdentityError(
        f"no canonical UUID mapping for tenant value {tenant_value!r} "
        "(OMN-15356) -- refusing to invent or default one; add an explicit "
        "entry to _LEGACY_TENANT_UUID_MAP only after confirming this is a "
        "real, reviewed tenant identity"
    )


def resolve_write_tenant(tenant_value: object, *, table: str) -> str:
    """Resolve the tenant a projection WRITE runs under.

    The GUC must equal what the database will actually store, so the only
    authorities are the row itself and, when the row omits ``tenant_id``, the
    column DEFAULT that Postgres then applies.

    Deliberately does NOT consult ``Settings.onex_tenant_id``: resolving a
    tenant from configuration is the HANDLER's job (it stamps
    ``row["tenant_id"]`` when it resolves one). Reading it again at this
    boundary would set the session to a tenant the stored row does not carry,
    and the policy rejects the write -- proven by execution against a real
    policy in OMN-15301.

    Raises :class:`TenantRequiredError` under enforcement, before any SQL is
    issued, so a refused write leaves zero rows.
    """
    if isinstance(tenant_value, str) and tenant_value.strip():
        return tenant_value.strip()
    require_tenant_id(None, table=table)
    return INTERIM_DEFAULT_TENANT


def house_tenant_write_stamp(*, table: str) -> dict[str, str]:
    """Return the ``tenant_id`` key a projection write should carry, or ``{}``.

    The one canonical implementation of the writer half of the house-tenant
    ruling (2026-08-02), so the eight relations classified TENANT by OMN-15655
    do not each grow their own copy of it.

    Resolution order, and why it is this order:

    1. ``Settings.onex_tenant_id`` when a lane configures one -- stamped
       explicitly, so the stored row carries the real tenant and the RLS GUC
       (which must equal what the database actually stored, per OMN-15301) has
       something true to be set to.
    2. Otherwise the key is OMITTED and Postgres' ``DEFAULT 'omninode'``
       supplies the HOUSE TENANT. Omitted, never ``None``: writing the key as
       NULL would violate ``NOT NULL`` and defeat the default -- the exact
       OMN-14058 writer-erasure shape.

    Step 2 is the "house-tenant default ONLY until customer ingress exists"
    half of the ruling. The "then missing attribution fails closed" half is
    :func:`require_tenant_id`, which this calls on the omit path: it is a no-op
    today and raises :class:`TenantRequiredError` the moment
    ``ENFORCE_TENANT_ISOLATION`` flips. The flip is a ratchet with a named
    condition, not a comment -- see
    ``tests/unit/projection/test_house_tenant_default_ratchet.py``, which pins
    the default-allowed state and states what has to become true to flip it.

    Called BEFORE the row is upserted so a refused write produces zero rows.
    """
    configured = get_settings().onex_tenant_id.strip()
    if configured:
        return {"tenant_id": configured}
    require_tenant_id(None, table=table)
    return {}


def resolve_read_tenant(tenant_value: object) -> str:
    """Resolve the tenant a projection READ runs under.

    Mirrors the HANDLER's resolution order (explicit value, then the lane's
    configured tenant, then the interim default) rather than the write path's
    column-default rule: where a lane configures a tenant, the handler stamped
    it onto the rows, so that is where existing-row probes must look.

    Never raises. An unresolvable read tenant already fails closed at the policy
    (zero rows visible, no leak), and raising here would break the probes that
    guard against clobbering already-written evidence.
    """
    if isinstance(tenant_value, str) and tenant_value.strip():
        return tenant_value.strip()
    return get_settings().onex_tenant_id.strip() or INTERIM_DEFAULT_TENANT


def require_tenant_id(tenant_id: str | None, *, table: str) -> None:
    """Raise :class:`TenantRequiredError` when isolation is enforced and blank.

    No-op (preserving the OMN-14058 default-fallback behavior verbatim)
    unless ``Settings.enforce_tenant_isolation`` is ``True``. Callers MUST
    invoke this before building/upserting the row so a refused write never
    reaches the database -- ``never produces a projection row`` per the
    OMN-14898 acceptance criterion.
    """
    if tenant_id is not None and tenant_id.strip():
        return
    if not get_settings().enforce_tenant_isolation:
        return
    raise TenantRequiredError(
        f"{table} write refused: no tenant_id resolved and "
        "ENFORCE_TENANT_ISOLATION=true (OMN-14898) -- refusing to let the "
        "write fall through to the shared tenant column default."
    )


__all__: list[str] = [
    "HOUSE_TENANT_SLUG",
    "HOUSE_TENANT_UUID",
    "INTERIM_DEFAULT_TENANT",
    "TENANT_GUC",
    "TenantRequiredError",
    "UnmappedTenantIdentityError",
    "house_tenant_write_stamp",
    "require_tenant_id",
    "resolve_read_tenant",
    "resolve_tenant_uuid",
    "resolve_write_tenant",
]
