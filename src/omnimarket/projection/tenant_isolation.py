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


class TenantContextMissingError(ValueError):
    """Raised when an RLS-covered READ cannot resolve a tenant (OMN-16092).

    The read-side counterpart of :class:`TenantRequiredError`. RLS fails OPEN
    from the caller's point of view: with the ``app.tenant_id`` GUC unset the
    policy predicate is NULL, so an RLS-covered ``SELECT`` returns ZERO ROWS
    rather than raising -- indistinguishable from "no data yet". Any consumer
    that treats an empty read as a benign signal (``roi_overlay`` degrading to
    static routing) therefore degrades SILENTLY on a blinded read.

    Raising this BEFORE the ``SELECT`` is issued converts that silent zero into
    a loud, typed failure the caller must handle explicitly. It is deliberately
    NOT caught by a reader's fail-open ``except Exception``: a telemetry outage
    (unreachable host, missing table) is fail-open by design, but a missing
    tenant seam is a correctness defect, not an outage.
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
#                      follow the identical mechanical pattern).
#                      :func:`resolve_tenant_uuid` is the single named
#                      implementation of that mapping. There is no separate
#                      SQL function: OMN-15732 AC2 adjudication (2026-08-08)
#                      rejected a standalone ``platform_catalog.
#                      house_tenant_map_slug_to_uuid`` catalog function as
#                      inadmissible for a node migration stream, so each
#                      conversion migration inlines the same closed mapping
#                      directly into its ``ALTER COLUMN ... USING`` clause.
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
# writer had produced at the time this module was first written was
# HOUSE_TENANT_SLUG (every DEFAULT, every backfill, every handler constant
# agreed -- see ``test_house_tenant_identity.py``). Re-deriving a UUID from an
# arbitrary input string would let any unreviewed value silently mint a new
# tenant identity with no registry entry and no RLS policy; this table can
# only ever resolve values this codebase already knows about, and everything
# else is refused. Extend this dict, never fall through to a computed
# default, when a second legacy value is discovered.
#
# OMN-15683: the two provisioned beta tenants. Live-verified against
# ``omninode_cloud.public.tenants`` on onex-dev (2026-08-03 probe recorded on
# the ticket, re-confirmed 2026-08-18): the gateway side
# (``gateway_workflows.tenant_id``, already UUID) and this table's read/write
# boundary must resolve the SAME identity for a receipt to be visible to its
# owning tenant. ``delegation_events.tenant_id`` (this table's TEXT slug
# column, migration 0022) stores these exact slugs today, stamped by
# ``stamp_verified_tenant_slug`` (omnibase_infra) from the gateway's
# config-bound ``ModelGatewayTenantIdentity`` -- the slug is not a guess, it
# is the wire value this codebase already writes.
_LEGACY_TENANT_UUID_MAP: dict[str, UUID] = {
    HOUSE_TENANT_SLUG: HOUSE_TENANT_UUID,
    "beta-business-proof": UUID("91c74442-1233-4c97-b191-911a10346fdf"),
    "beta-gateway-canary-79afa7263852": UUID("79afa726-3852-464f-b7a4-d4b8b9c75ee7"),
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
    TENANT relation is derived from: each conversion migration (starting
    with migration 0003 of ``node_canary_score_reducer``) inlines the
    identical closed mapping directly into its own ``USING`` clause rather
    than calling a shared SQL function (OMN-15732 AC2 adjudication,
    2026-08-08).
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


def resolve_tenant_uuid_or_none(tenant_value: str | None) -> str | None:
    """Null-safe wrapper around :func:`resolve_tenant_uuid` for writer sites
    that stamp a UUID-typed ``tenant_id`` column (OMN-15683).

    Writers on this projection surface follow the OMN-14058 interim pattern:
    "only stamp the key when the source event carried a value -- omitting it
    lets the column DEFAULT apply on INSERT and leaves an already-known
    tenant untouched on UPDATE." That pattern needs a resolver that returns
    ``None`` (not a raise, not an invented value) for the blank/absent case,
    while still failing loudly on a PRESENT-but-unrecognized slug --
    :func:`resolve_tenant_uuid` already does the latter, so this only adds
    the former. A relation whose ``tenant_id`` column is still TEXT (e.g.
    ``delegation_budget_state``, ``delegation_judge_verdict_events``) must
    NOT route through this function -- it would coerce a valid TEXT-column
    slug into a UUID string the column was never migrated to accept.
    """
    if not tenant_value:
        return None
    return str(resolve_tenant_uuid(tenant_value))


# OMN-15683: relations whose tenant_id column has been converted from the
# legacy TEXT slug to the canonical UUID (OMN-15356's classified-TENANT
# sweep). The interim-default GUC fallback below must equal whichever
# representation the table's OWN column DEFAULT/RLS cast now expects --
# see resolve_write_tenant's "the GUC must equal what the database will
# actually store" rule, which this set exists to keep true after a
# conversion. Extend only when a relation's own migration converts its
# column (never speculatively): delegation_budget_state,
# delegation_judge_verdict_events, and every other tenant_id column on this
# surface remain TEXT today and must NOT be added here.
_UUID_CONVERTED_TABLES: frozenset[str] = frozenset({"delegation_events"})


def _house_tenant_interim_default(table: str | None) -> str:
    """The house-tenant fallback value, in whichever representation
    ``table``'s own column currently expects."""
    if table in _UUID_CONVERTED_TABLES:
        return str(HOUSE_TENANT_UUID)
    return INTERIM_DEFAULT_TENANT


def resolve_write_tenant(tenant_value: object, *, table: str) -> str:
    """Resolve the tenant a projection WRITE runs under.

    The GUC must equal what the database will actually store, so the only
    authorities are the row itself and, when the row omits ``tenant_id``, the
    column DEFAULT that Postgres then applies -- which is why the fallback
    below is table-aware (:data:`_UUID_CONVERTED_TABLES`): for a converted
    relation the column DEFAULT is now the house tenant UUID, not the slug,
    and the RLS policy casts the GUC to ``::uuid`` -- setting it to the slug
    would fail that cast outright (OMN-15683).

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
    return _house_tenant_interim_default(table)


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


def resolve_read_tenant(tenant_value: object, *, table: str | None = None) -> str:
    """Resolve the tenant a projection READ runs under.

    Mirrors the HANDLER's resolution order (explicit value, then the lane's
    configured tenant, then the interim default) rather than the write path's
    column-default rule: where a lane configures a tenant, the handler stamped
    it onto the rows, so that is where existing-row probes must look.

    ``table`` is OPTIONAL (unlike :func:`resolve_write_tenant`, where it is
    required): this function is also called from the generic
    ``AsyncpgAdapter._set_tenant_context`` fallback, which has no table in
    scope at all. Passing it (as ``PostgresSyncProjectionAdapter.query()``
    does) makes the interim-default fallback table-aware
    (:data:`_UUID_CONVERTED_TABLES`, OMN-15683) so it matches whichever
    representation that table's column now expects; omitting it preserves
    the pre-OMN-15683 slug fallback for every other caller.

    Never raises. An unresolvable read tenant already fails closed at the policy
    (zero rows visible, no leak), and raising here would break the probes that
    guard against clobbering already-written evidence.
    """
    if isinstance(tenant_value, str) and tenant_value.strip():
        return tenant_value.strip()
    return get_settings().onex_tenant_id.strip() or _house_tenant_interim_default(table)


def resolve_rls_read_tenant(tenant_value: object, *, table: str) -> str:
    """Resolve the tenant an RLS-covered READ runs under -- FAIL-CLOSED (OMN-16092).

    Same resolution ORDER as :func:`resolve_read_tenant` (explicit value, then
    the lane's configured tenant, then the house tenant), and the same ratchet
    posture as the writer's :func:`require_tenant_id`: the house-tenant fallback
    is allowed only while ``ENFORCE_TENANT_ISOLATION`` is off. Under enforcement
    an unresolvable tenant raises :class:`TenantContextMissingError` instead.

    Why this exists alongside :func:`resolve_read_tenant`, which never raises:
    that function serves the WRITER's existing-row probe, where raising would
    break the very check that guards against clobbering already-written evidence
    -- an unresolvable tenant there is safe because the probe's caller is about
    to fail closed on the write anyway. This one serves readers whose RESULT is
    the product (``context_roi_scores`` -> the routing ROI overlay), where an
    unresolvable tenant would otherwise produce a plausible-looking empty result
    set that the caller cannot distinguish from real emptiness.

    Called BEFORE any connection or SQL, so a refused read issues no statement.
    """
    if isinstance(tenant_value, str) and tenant_value.strip():
        return tenant_value.strip()
    configured = get_settings().onex_tenant_id.strip()
    if configured:
        return configured
    if get_settings().enforce_tenant_isolation:
        raise TenantContextMissingError(
            f"{table} read refused: no tenant_id resolved and "
            "ENFORCE_TENANT_ISOLATION=true (OMN-16092) -- refusing to issue an "
            "RLS-covered SELECT with no tenant context, which would return zero "
            "rows indistinguishable from an empty table."
        )
    return INTERIM_DEFAULT_TENANT


def resolve_serving_tenant(tenant_value: object, *, topic: str) -> str:
    """Resolve the tenant an HTTP projection READ is served under (OMN-15797 AC2).

    Resolution order: the caller's explicit value, then the lane's configured
    ``Settings.onex_tenant_id``. There is deliberately **no third step**.

    Every other resolver in this module ends in a house-tenant fallback, and
    each is right to: :func:`resolve_read_tenant` serves the writer's own
    existing-row probe, and :func:`resolve_rls_read_tenant` serves an
    in-process runtime read whose tenant the lane itself owns. This one serves
    an EXTERNAL caller over HTTP. Falling back to the house tenant there would
    answer ``200`` with the house tenant's rows to a caller who supplied no
    tenant at all -- a plausible-looking result indistinguishable from a
    correctly-scoped one, which is precisely the silent-answer property
    OMN-15797 exists to make unrepresentable. So this raises instead, and the
    serving path maps the raise to a typed refusal.

    Unlike :func:`resolve_rls_read_tenant`, the refusal is NOT gated on
    ``ENFORCE_TENANT_ISOLATION``: that ratchet exists to avoid rejecting real
    single-tenant traffic in lanes with no tenant configured, and it applies to
    surfaces that are reached whether or not anyone opted into scoping. This
    one is reached only for an exposure whose contract explicitly declares a
    ``tenant_column`` -- the opt-in IS the contract declaration, so a second
    env-var ratchet in front of it would only make the declaration a no-op.

    Raises:
        TenantContextMissingError: no tenant could be resolved.
    """
    if isinstance(tenant_value, str) and tenant_value.strip():
        return tenant_value.strip()
    configured = get_settings().onex_tenant_id.strip()
    if configured:
        return configured
    raise TenantContextMissingError(
        f"{topic} refused: no tenant context resolved (OMN-15797) -- the "
        "exposure declares a tenant_column, so serving it unscoped would "
        "return either another tenant's rows or an empty page the caller "
        "cannot distinguish from real emptiness. Supply ?tenant=<id> or "
        "configure ONEX_TENANT_ID for this lane."
    )


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
    "TenantContextMissingError",
    "TenantRequiredError",
    "UnmappedTenantIdentityError",
    "house_tenant_write_stamp",
    "require_tenant_id",
    "resolve_read_tenant",
    "resolve_rls_read_tenant",
    "resolve_serving_tenant",
    "resolve_tenant_uuid",
    "resolve_tenant_uuid_or_none",
    "resolve_write_tenant",
]
