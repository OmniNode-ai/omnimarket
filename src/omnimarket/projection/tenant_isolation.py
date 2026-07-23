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


__all__: list[str] = ["TenantRequiredError", "require_tenant_id"]
