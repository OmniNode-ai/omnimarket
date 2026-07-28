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

from omnimarket.config.settings import get_settings


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

# The DEFAULT on every landed tenant_id column (0019/0022, savings 080). When an
# event resolves no tenant the row takes this value, so the GUC must match it or
# the policy's WITH CHECK rejects a write it would otherwise accept.
INTERIM_DEFAULT_TENANT = "omninode"


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
    "INTERIM_DEFAULT_TENANT",
    "TENANT_GUC",
    "TenantRequiredError",
    "require_tenant_id",
    "resolve_read_tenant",
    "resolve_write_tenant",
]
