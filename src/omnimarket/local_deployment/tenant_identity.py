# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18699: the tenant identity a LOCAL install mints for itself.

The 2026-09-08 ruling, firm: tenant identity is deployment-scoped. A local
deployment carries its own identifier and must not embed or depend on a cloud
one, and the shared hardcoded house-tenant constant is a defect wherever it acts
as a fallback -- the house tenant must resolve per deployment, and fail fast.

On a local path the customer's machine IS the deployment. So:

* ``onex local init`` mints a UUID **once**, at install / first run, and records
  it in the local store under :data:`LOCAL_TENANT_IDENTITY_KEY`.
* Every local delegation reads that identity back by reference. It is never
  passed on the wire, never read from an environment variable that a caller
  controls, and never derived from anything guessable -- two installs on two
  machines mint two identities (AC4), which a hostname- or user-derived value
  would not guarantee.
* A local run with no identity is a typed REFUSAL naming the init command. It is
  not a run under the house tenant. That substitution is what this module
  removes: before it, ``port_local_delegation_dispatch`` stamped
  ``tenant_id or HOUSE_TENANT_SLUG`` and every local evidence row on this
  machine landed under the house tenant whether or not one had been resolved.

**The minted identity is confirmed, not asserted.** ``resolve_registry_tenant_uuid``
(OMN-16831) refuses a UUID that ``tenant_registry_mirror`` holds no row for --
deliberately, so that no writer can attribute a row to an identifier nobody
recorded. The mint therefore records the identity in the local store's own
``tenant_registry_mirror`` as well, which is the SAME relation the deployed
resolver confirms a cloud tenant against. The local path gains no bypass and no
second resolver: it satisfies the existing one.

**Nothing here is a default.** Every function either returns an identity that was
explicitly minted, or refuses. :func:`local_tenant_identity_or_none` is the one
non-raising reader, and it exists for the shared orchestrator handler that serves
BOTH the bus and the local path: a bus runtime has no local store, so it reads
``None`` there and behaves exactly as it did before this module existed.

The store is the EXISTING local delegation sqlite file -- the same one the local
evidence rows live in. No new store, no new file, no new directory.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from enum import StrEnum, unique
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.sqlite_database import (
    SqliteDatabaseAdapter,
    default_evidence_db_path,
)
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
)

__all__ = [
    "INIT_COMMAND",
    "LOCAL_DEPLOYMENT_IDENTITY_TABLE",
    "LOCAL_TENANT_IDENTITY_KEY",
    "EnumLocalTenantIdentityRefusalReason",
    "LocalTenantIdentityError",
    "ModelLocalTenantIdentity",
    "ModelLocalTenantIdentityRefusal",
    "ensure_install_identity_mirrored",
    "install_identity_store",
    "local_tenant_identity_or_none",
    "mint_local_tenant_identity",
    "read_local_tenant_identity",
    "require_local_tenant_identity",
    "reset_local_tenant_identity_cache",
    "resolve_local_deployment_tenant_id",
]

#: The relation the deployment's own identity is recorded in, inside the
#: existing local delegation sqlite file. A key/value shape rather than a
#: one-column table so a later deployment-scoped fact has a declared home
#: without another migration-shaped decision.
LOCAL_DEPLOYMENT_IDENTITY_TABLE: Final[str] = "local_deployment_identity"

#: The declared key the deployment's tenant identity is stored under. Callers
#: read by this name; nothing reads position or "the only row".
LOCAL_TENANT_IDENTITY_KEY: Final[str] = "local_deployment.tenant_id"

#: Named once so every refusal points at the same remedy.
INIT_COMMAND: Final[str] = "onex local init"

_IDENTITY_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {LOCAL_DEPLOYMENT_IDENTITY_TABLE} (
    key         TEXT NOT NULL UNIQUE,
    value       TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""

#: Mirrors the columns ``node_projection_tenant_registry``'s migration 0000
#: declares, in sqlite's type vocabulary. Only the two the resolver reads are
#: NOT NULL; the rest exist so a local row and a deployed row are the same row
#: shape rather than two shapes that happen to answer one query.
_MIRROR_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {TENANT_REGISTRY_MIRROR_TABLE} (
    tenant_slug         TEXT NOT NULL UNIQUE,
    tenant_uuid         TEXT NOT NULL,
    display_name        TEXT,
    status              TEXT NOT NULL,
    registry_created_at TEXT,
    observed_at         TEXT NOT NULL,
    source_event_id     TEXT
)
"""

#: What the mint records as the local registry row's provenance. A local install
#: is its own registrar; naming that is honest, where borrowing the tenant
#: projection node's name would claim an event stream that never ran.
_LOCAL_MINT_SOURCE: Final[str] = "omn18699:local-install-mint"


@unique
class EnumLocalTenantIdentityRefusalReason(StrEnum):
    """Why a local deployment could not resolve its own tenant identity."""

    #: No identity has ever been minted on this install.
    IDENTITY_ABSENT = "identity_absent"
    #: An identity is recorded but is not a UUID. Never silently re-minted: a
    #: corrupted identity and an absent one call for different repairs.
    IDENTITY_MALFORMED = "identity_malformed"
    #: A different identity was named for an install that already has one.
    IDENTITY_CONFLICT = "identity_conflict"


_REMEDIATION: Final[dict[EnumLocalTenantIdentityRefusalReason, str]] = {
    EnumLocalTenantIdentityRefusalReason.IDENTITY_ABSENT: (
        f"Run `{INIT_COMMAND}` once to mint this install's tenant identity."
    ),
    EnumLocalTenantIdentityRefusalReason.IDENTITY_MALFORMED: (
        f"Run `{INIT_COMMAND} --tenant-id <uuid>` naming the identity this "
        "install's existing records already carry."
    ),
    EnumLocalTenantIdentityRefusalReason.IDENTITY_CONFLICT: (
        "This install already has an identity and it does not move. Re-run "
        f"`{INIT_COMMAND}` with no --tenant-id to see it."
    ),
}


class ModelLocalTenantIdentityRefusal(BaseModel):
    """A typed, non-retryable refusal from the local tenant-identity seam.

    ``retryable`` is derived rather than settable: a caller that could set it
    could set it wrongly, and a missing identity is a configuration fact that
    re-asking the same question cannot turn into a present one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: EnumLocalTenantIdentityRefusalReason = Field(
        description="Which identity condition refused this local run."
    )
    detail: str = Field(
        description="What was observed, in the operator's terms. Never a secret."
    )

    @property
    def retryable(self) -> bool:
        """Always ``False``. See the class docstring."""
        return False

    @property
    def remediation(self) -> str:
        """The single action that clears this refusal."""
        return _REMEDIATION[self.reason]

    @property
    def message(self) -> str:
        """The one sentence a caller shows a human."""
        return (
            f"OMN-18699 {self.reason.value}: {self.detail} {self.remediation} "
            "This local deployment records under its own tenant identity; the "
            "shared house tenant is not a fallback."
        )


class LocalTenantIdentityError(RuntimeError):
    """Raised when a local run has no resolvable deployment tenant identity."""

    def __init__(self, refusal: ModelLocalTenantIdentityRefusal) -> None:
        super().__init__(refusal.message)
        self.refusal = refusal


class ModelLocalTenantIdentity(BaseModel):
    """The identity this install records under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_uuid: UUID = Field(description="The deployment-scoped tenant identity.")
    tenant_slug: str = Field(
        description="The mirror's primary key for this identity. Local by default."
    )
    recorded_at: datetime = Field(description="When this install recorded it.")
    newly_minted: bool = Field(
        default=False,
        description="True only for the call that created it, so init can report "
        "'minted' and 'already present' differently.",
    )


# The identity is read on every local delegation and never changes within a
# process, so it is cached per store path. Keyed by the resolved path rather
# than globally: the tests drive several stores in one process, and a global
# cache would make the second one read the first one's answer.
_CACHE: dict[Path, ModelLocalTenantIdentity] = {}
_CACHE_LOCK = threading.Lock()


def reset_local_tenant_identity_cache() -> None:
    """Drop the per-path identity cache. For tests, and for ``init`` itself."""
    with _CACHE_LOCK:
        _CACHE.clear()


def install_identity_store() -> Path:
    """Where THIS install records its identity.

    The identity is a property of the INSTALL, not of whichever file a given
    run happens to write its evidence rows into. A caller that redirects only
    the evidence target -- an overlay pointing at Postgres, a test using a
    tmp_path store -- has not changed which deployment this is, so it must not
    change which identity the deployment records under. So this is the one
    well-known location, and it is the existing local delegation store rather
    than a new file.
    """
    return default_evidence_db_path()


def _resolve_db_path(db_path: Path | None) -> Path:
    return db_path if db_path is not None else install_identity_store()


def _adapter(db_path: Path) -> SqliteDatabaseAdapter:
    return SqliteDatabaseAdapter(db_path)


def _ensure_tables(db_path: Path) -> None:
    """Create this module's two relations idempotently.

    ``SqliteDatabaseAdapter`` creates ``delegation_events`` on connect and adds
    columns additively, but it declares no table of its own for these two, and a
    query against a table that does not exist returns ``[]`` rather than raising
    -- so an absent table would read as an absent identity and the mint would
    then have nowhere to put one.
    """
    _ensure_tables_on(_adapter(db_path))


def _ensure_tables_on(adapter: SqliteDatabaseAdapter) -> None:
    """Create this module's two relations on an adapter that is already open."""
    conn = adapter._connect()  # noqa: SLF001 - this module owns these two relations
    try:
        conn.execute(_IDENTITY_DDL)
        conn.execute(_MIRROR_DDL)
        conn.commit()
    finally:
        conn.close()


def read_local_tenant_identity(
    *, db_path: Path | None = None
) -> ModelLocalTenantIdentity | None:
    """Return this install's recorded identity, or ``None`` if it has none.

    Raises :class:`LocalTenantIdentityError` only for a recorded-but-unreadable
    value: "nothing was ever minted" and "what was minted is corrupt" are
    different facts and are never collapsed into one.
    """
    resolved = _resolve_db_path(db_path)
    with _CACHE_LOCK:
        cached = _CACHE.get(resolved)
    if cached is not None:
        return cached

    if not resolved.exists():
        return None

    rows = _adapter(resolved).query(
        LOCAL_DEPLOYMENT_IDENTITY_TABLE, {"key": LOCAL_TENANT_IDENTITY_KEY}
    )
    if not rows:
        return None

    raw = rows[0].get("value")
    try:
        tenant_uuid = UUID(str(raw))
    except (TypeError, ValueError) as exc:
        raise LocalTenantIdentityError(
            ModelLocalTenantIdentityRefusal(
                reason=EnumLocalTenantIdentityRefusalReason.IDENTITY_MALFORMED,
                detail=(
                    f"the local store records {raw!r} under "
                    f"{LOCAL_TENANT_IDENTITY_KEY!r}, which is not a UUID."
                ),
            )
        ) from exc

    recorded_raw = rows[0].get("recorded_at")
    try:
        recorded_at = datetime.fromisoformat(str(recorded_raw))
    except (TypeError, ValueError):
        # A missing or unparseable timestamp is bookkeeping, not identity. The
        # identity itself is intact, so the read succeeds and reports the epoch
        # rather than refusing a run over a column nothing decides on.
        recorded_at = datetime.fromtimestamp(0, tz=UTC)

    mirror = _adapter(resolved).query(
        TENANT_REGISTRY_MIRROR_TABLE, {"tenant_uuid": str(tenant_uuid)}
    )
    tenant_slug = (
        str(mirror[0]["tenant_slug"]) if mirror else _default_slug(tenant_uuid)
    )

    identity = ModelLocalTenantIdentity(
        tenant_uuid=tenant_uuid,
        tenant_slug=tenant_slug,
        recorded_at=recorded_at,
    )
    with _CACHE_LOCK:
        _CACHE[resolved] = identity
    return identity


def _default_slug(tenant_uuid: UUID) -> str:
    """Derive the mirror key from the identity itself.

    Deliberately NOT from the hostname or the username: the slug is the mirror's
    primary key, and deriving it from a machine fact would make two installs on
    two similarly-named machines collide on it.
    """
    return f"local-{tenant_uuid.hex[:12]}"


def local_tenant_identity_or_none(*, db_path: Path | None = None) -> str | None:
    """The non-raising reader, for the handler that serves BOTH paths.

    A bus runtime has no local store, so this reads ``None`` there and the
    handler's precedence chain behaves exactly as it did before OMN-18699. It
    returns a string because every caller stamps a wire field.
    """
    try:
        identity = read_local_tenant_identity(db_path=db_path)
    except LocalTenantIdentityError:
        # A corrupt local identity must not take down a bus runtime that never
        # asked for one. The LOCAL path calls require_* and still refuses.
        return None
    return str(identity.tenant_uuid) if identity is not None else None


def require_local_tenant_identity(*, db_path: Path | None = None) -> UUID:
    """This install's identity, or a typed refusal. Never the house tenant."""
    identity = read_local_tenant_identity(db_path=db_path)
    if identity is None:
        raise LocalTenantIdentityError(
            ModelLocalTenantIdentityRefusal(
                reason=EnumLocalTenantIdentityRefusalReason.IDENTITY_ABSENT,
                detail=(
                    "this install has never minted a tenant identity, so there "
                    "is nothing to attribute this run to."
                ),
            )
        )
    return identity.tenant_uuid


def resolve_local_deployment_tenant_id(
    tenant_id: str | None, *, db_path: Path | None = None
) -> str:
    """The tenant a LOCAL delegation records under. Refuses rather than defaults.

    Precedence, unchanged at the top and closed at the bottom:

    1. a tenant verified upstream and threaded in by the caller;
    2. this install's own minted identity;
    3. nothing -- a typed refusal.

    Step 3 used to be the house-tenant constant, which is what made every local
    row on an un-initialised install indistinguishable from an OmniNode one.
    """
    verified = (tenant_id or "").strip()
    if verified:
        return verified
    return str(require_local_tenant_identity(db_path=db_path))


def ensure_install_identity_mirrored(db: object) -> None:
    """Record THIS install's identity in a local sqlite evidence target's mirror.

    ``resolve_registry_tenant_uuid`` confirms a UUID identity against the
    ``tenant_registry_mirror`` relation of the store being written to, and
    refuses when it holds no row -- deliberately, so no writer attributes a row
    to an identifier nobody recorded. ``onex local init`` writes that row into
    the install's own store, which IS the evidence target in the ordinary case.
    A caller that redirects the evidence target to a DIFFERENT sqlite file (a
    test, a second local store) would otherwise hold an identity that resolves
    in one file and refuses in the other.

    Two bounds make this a record rather than a bypass:

    * it mirrors ONLY this install's own minted identity, never a tenant handed
      in by a caller -- a gateway-verified cloud tenant still has to be in the
      mirror by the means that put it there, and refuses here if it is not;
    * it does nothing unless the target is a local sqlite evidence file. It
      never writes a registry row into a deployed store, which is
      ``node_projection_tenant_registry``'s job and not this module's.
    """
    if not isinstance(db, SqliteDatabaseAdapter):
        return
    identity = read_local_tenant_identity()
    if identity is None:
        return
    # The relation may not exist in a redirected store; a query against an
    # absent sqlite table reads as zero rows rather than raising, so creating it
    # here is what turns "no row" into a fact rather than an artifact.
    _ensure_tables_on(db)
    existing = db.query(
        TENANT_REGISTRY_MIRROR_TABLE, {"tenant_uuid": str(identity.tenant_uuid)}
    )
    if existing:
        return
    _write_mirror_row(
        db,
        tenant_uuid=identity.tenant_uuid,
        tenant_slug=identity.tenant_slug,
        now=datetime.now(tz=UTC),
    )


def _write_mirror_row(
    adapter: SqliteDatabaseAdapter,
    *,
    tenant_uuid: UUID,
    tenant_slug: str,
    now: datetime,
) -> None:
    adapter.upsert(
        TENANT_REGISTRY_MIRROR_TABLE,
        "tenant_slug",
        {
            "tenant_slug": tenant_slug,
            "tenant_uuid": str(tenant_uuid),
            "display_name": "local install",
            "status": "active",
            "registry_created_at": now.isoformat(),
            "observed_at": now.isoformat(),
            "source_event_id": _LOCAL_MINT_SOURCE,
        },
    )


def mint_local_tenant_identity(
    *,
    db_path: Path | None = None,
    tenant_uuid: UUID | None = None,
    tenant_slug: str | None = None,
) -> ModelLocalTenantIdentity:
    """Mint this install's tenant identity once. Idempotent.

    ``tenant_uuid`` is for an install that ALREADY has records under a known
    identity -- naming it keeps those records and the new ones on one tenant.
    Omitted, a fresh ``uuid4`` is minted: random rather than derived, so two
    installs cannot land on one identity (AC4) and no machine fact leaks into
    the identifier.

    Re-running with no ``tenant_uuid`` returns the existing identity untouched.
    Re-running with a DIFFERENT one refuses: an install's identity does not
    move, and silently repointing it would orphan every record already written.
    """
    resolved = _resolve_db_path(db_path)
    _ensure_tables(resolved)
    reset_local_tenant_identity_cache()

    existing = read_local_tenant_identity(db_path=resolved)
    if existing is not None:
        if tenant_uuid is not None and tenant_uuid != existing.tenant_uuid:
            raise LocalTenantIdentityError(
                ModelLocalTenantIdentityRefusal(
                    reason=EnumLocalTenantIdentityRefusalReason.IDENTITY_CONFLICT,
                    detail=(
                        f"this install already records under {existing.tenant_uuid}, "
                        f"and {tenant_uuid} was named instead."
                    ),
                )
            )
        return existing

    minted_uuid = tenant_uuid if tenant_uuid is not None else uuid4()
    minted_slug = tenant_slug or _default_slug(minted_uuid)
    now = datetime.now(tz=UTC)
    adapter = _adapter(resolved)

    # The mirror row FIRST: it is what makes the identity resolvable. An
    # identity recorded without one would pass this module's own read and then
    # be refused by the shared registry resolver at the first write -- an
    # install that looks initialised and cannot record anything.
    _write_mirror_row(
        adapter, tenant_uuid=minted_uuid, tenant_slug=minted_slug, now=now
    )
    adapter.upsert(
        LOCAL_DEPLOYMENT_IDENTITY_TABLE,
        "key",
        {
            "key": LOCAL_TENANT_IDENTITY_KEY,
            "value": str(minted_uuid),
            "recorded_at": now.isoformat(),
        },
    )
    reset_local_tenant_identity_cache()

    return ModelLocalTenantIdentity(
        tenant_uuid=minted_uuid,
        tenant_slug=minted_slug,
        recorded_at=now,
        newly_minted=True,
    )
