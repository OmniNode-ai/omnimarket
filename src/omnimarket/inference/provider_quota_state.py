# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provider quota state, read from the durable projection (OMN-20154).

OMN-16932 closed the loop between a provider's 429 and routing with a
process-local, in-memory ledger. That ledger was lost on every restart, was
invisible to every other process (the in-process ``onex delegate`` path, the
judge in another container, a second runtime replica) and could not be queried
by anyone. It was also never written on the runtime's own inference path, so
the lab runtime never learned a z.ai 1302 at all.

The state now lives where the rest of the platform's truth lives: quota
observations are emitted as events (``omnimarket.events.provider_quota``), the
``node_projection_provider_quota`` writer folds them into
``public.provider_quota_state``, and this module READS that table. Routing and
the judge each take a :class:`ModelProviderQuotaSnapshot`, read once per
decision, as a pure input.

Key: ``(tenant_id, credential_ref, provider_id, model_scope)``. ``provider_id``
is the quota domain (the policy's ``provider_id`` for a declared host, the
prefixed host otherwise), so every backend spending one provider counter shares
one key. ``model_scope`` is ``*`` for a provider-wide refusal and the model
name for a per-model one. The lab is one tenant here like any other.

Fail direction:

* An observation that says nothing about capacity never blocks anything.
* A block lifts at the provider's stated instant with no operator action; a
  ``disable_until_billing`` block lifts only on a later successful call.
* **Unknown fails closed.** A snapshot that could not be read (no reader, a
  database error) bars every METERED provider, meaning every provider the
  quota policy declares, and never an undeclared host such as a local model.
  Calling a paid provider blind is exactly the failure this state exists to
  prevent; the local rungs keep the ladder alive meanwhile.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.provider_quota import (
    PROVIDER_WIDE_MODEL_SCOPE,
    EnumProviderQuotaOutcome,
    ModelProviderQuotaObserved,
    credential_ref_for,
)
from omnimarket.inference.provider_quota_policy import load_provider_quota_policy
from omnimarket.projection.tenant_isolation import (
    TENANT_GUC,
    resolve_rls_read_tenant,
    resolve_tenant_uuid,
)

if TYPE_CHECKING:
    import psycopg2  # type: ignore[import-untyped]

_logger = logging.getLogger(__name__)

#: The projection table (tenant-scoped, RLS-covered).
PROVIDER_QUOTA_STATE_RELATION = "public.provider_quota_state"
_TENANT_TABLE = "provider_quota_state"

#: The projection DB DSN the lane's other routing reads use (the DoD overlay
#: and the tenant overlay read ``delegation_events`` through the same one).
_ENV_DSN = "OMNIDASH_ANALYTICS_DB_URL"

_CONNECT_TIMEOUT_SECONDS = 3
_RELATION_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

#: ``disposition`` of the synthetic block an unreadable snapshot returns.
UNKNOWN_QUOTA_STATE_DISPOSITION = "quota_state_unknown"


class ModelProviderQuotaBlock(BaseModel):
    """One active block on one quota key, as the projection holds it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    credential_ref: str
    provider_id: str
    model_scope: str
    disposition: str
    blocked_until: datetime | None = Field(
        default=None, description="None only with blocked_indefinitely."
    )
    blocked_indefinitely: bool = False
    provider_code: str | None = None
    reason: str = ""

    def active_at(self, instant: datetime) -> bool:
        if self.blocked_indefinitely:
            return True
        return self.blocked_until is not None and instant < self.blocked_until

    @property
    def quota_domain(self) -> str:
        """Kept for the routing-exclusion report, which names the domain."""
        return self.provider_id

    @property
    def disabled_until(self) -> datetime | None:
        """The OMN-16932 name for ``blocked_until``, read by the exclusion report."""
        return self.blocked_until


class ModelProviderQuotaSnapshot(BaseModel):
    """The active blocks of one tenant, read once, evaluated as of one instant.

    Evaluating against ``as_of`` rather than a live clock keeps every decision
    made from one snapshot mutually consistent and replayable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID | None
    as_of: datetime
    readable: bool
    unreadable_reason: str = ""
    blocks: tuple[ModelProviderQuotaBlock, ...] = ()

    @classmethod
    def empty(
        cls, *, as_of: datetime, tenant_id: UUID | None = None
    ) -> ModelProviderQuotaSnapshot:
        """A readable snapshot holding no block (no refusal has been observed)."""
        return cls(tenant_id=tenant_id, as_of=as_of, readable=True)

    @classmethod
    def unknown(
        cls, *, as_of: datetime, reason: str, tenant_id: UUID | None = None
    ) -> ModelProviderQuotaSnapshot:
        """A snapshot that could not be read. Bars every metered provider."""
        return cls(
            tenant_id=tenant_id,
            as_of=as_of,
            readable=False,
            unreadable_reason=reason,
        )

    def with_observation(
        self, observation: ModelProviderQuotaObserved | None
    ) -> ModelProviderQuotaSnapshot:
        """This snapshot plus the block a just-made observation records.

        The caller emitted ``observation`` moments ago and the projection has
        not folded it yet; a decision taken in the same breath must not route
        straight back into the refusal it just observed. Pure: the block is
        derived from the event, exactly as the projection will derive it.
        """
        if (
            observation is None
            or observation.outcome is not EnumProviderQuotaOutcome.LIMIT_HIT
        ):
            return self
        block = ModelProviderQuotaBlock(
            credential_ref=observation.credential_ref,
            provider_id=observation.provider_id,
            model_scope=(
                PROVIDER_WIDE_MODEL_SCOPE
                if observation.block_scope == "provider"
                else observation.model_name
            ),
            disposition=observation.disposition or "",
            blocked_until=observation.blocked_until,
            blocked_indefinitely=observation.blocked_indefinitely,
            provider_code=observation.provider_code,
            reason=observation.reason,
        )
        return self.model_copy(update={"blocks": (*self.blocks, block)})

    def block_for(
        self,
        *,
        provider_id: str,
        credential_ref: str,
        model_name: str,
        metered: bool,
    ) -> ModelProviderQuotaBlock | None:
        """The block that bars this key, or ``None`` when it is routable."""
        if not self.readable:
            if not metered:
                return None
            return ModelProviderQuotaBlock(
                credential_ref=credential_ref,
                provider_id=provider_id,
                model_scope=PROVIDER_WIDE_MODEL_SCOPE,
                disposition=UNKNOWN_QUOTA_STATE_DISPOSITION,
                blocked_indefinitely=True,
                reason=(
                    "provider quota state is unreadable, so a metered provider "
                    f"is not called blind: {self.unreadable_reason}"
                ),
            )
        for block in self.blocks:
            if (
                block.provider_id == provider_id
                and block.credential_ref == credential_ref
                and block.model_scope in (PROVIDER_WIDE_MODEL_SCOPE, model_name)
                and block.active_at(self.as_of)
            ):
                return block
        return None


def _declared_provider_for_host(host: str) -> str | None:
    policy = load_provider_quota_policy()
    for provider in policy.providers:
        match = provider.match_endpoint_host.lower()
        if host == match or host.endswith(f".{match}"):
            return provider.provider_id
    return None


def quota_domain_for_endpoint(endpoint_url: str) -> str | None:
    """Return the quota failure domain an endpoint belongs to.

    Prefers the contract-declared ``provider_id`` so every backend sharing one
    provider's counter shares one key. Falls back to the endpoint host
    (prefixed, so it can never collide with a declared provider_id) when the
    policy does not describe the host. Returns ``None`` for a URL with no host.
    A policy that cannot be loaded raises: the policy is what says which
    providers are metered, and guessing would decide the fail-closed set.
    """
    host = (urlparse(endpoint_url).hostname or "").lower()
    if not host:
        return None
    declared = _declared_provider_for_host(host)
    return declared if declared is not None else f"host:{host}"


def endpoint_is_metered(endpoint_url: str) -> bool:
    """Whether the quota policy declares this endpoint's provider."""
    host = (urlparse(endpoint_url).hostname or "").lower()
    return bool(host) and _declared_provider_for_host(host) is not None


def quota_block_for_backend(
    snapshot: ModelProviderQuotaSnapshot | None,
    *,
    endpoint_url: str,
    api_key_ref: str | None,
    model_name: str,
) -> ModelProviderQuotaBlock | None:
    """The block that bars this backend under ``snapshot``, or ``None``.

    ``snapshot=None`` means the caller took no quota input at all (an offline
    replay, a structural question such as "is there a next tier"); it bars
    nothing. The deployed routing consumer and the judge always pass one.
    """
    if snapshot is None:
        return None
    domain = quota_domain_for_endpoint(endpoint_url)
    if domain is None:
        return None
    return snapshot.block_for(
        provider_id=domain,
        credential_ref=credential_ref_for(api_key_ref),
        model_name=model_name,
        metered=not domain.startswith("host:"),
    )


def resolve_quota_tenant(tenant_value: object) -> UUID:
    """The tenant UUID a quota row is keyed and read under.

    Producers and readers both call this, so a delegation with no explicit
    tenant is keyed under the lane's configured tenant on both sides. Raises
    under tenant enforcement when nothing resolves (never a guessed tenant).
    """
    if isinstance(tenant_value, UUID):
        return tenant_value
    tenant = resolve_rls_read_tenant(
        str(tenant_value) if tenant_value is not None else None, table=_TENANT_TABLE
    )
    try:
        return UUID(tenant)
    except ValueError:
        return resolve_tenant_uuid(tenant)


def active_blocks_sql(relation: str = PROVIDER_QUOTA_STATE_RELATION) -> str:
    """The one read routing depends on: a tenant's blocks still in force.

    psycopg2 named parameters ``tenant_id`` and ``as_of``. A row whose block
    has lifted, or whose last successful call cleared it (``disposition`` is
    NULL), is not returned.
    """
    if not _RELATION_PATTERN.match(relation):
        raise ValueError(f"unsafe relation identifier: {relation!r}")
    return (
        "SELECT credential_ref, provider_id, model_scope, disposition, "
        "blocked_until, blocked_indefinitely, last_provider_code, block_reason "
        f"FROM {relation} "
        "WHERE tenant_id = %(tenant_id)s::uuid "
        "AND disposition IS NOT NULL "
        "AND (blocked_indefinitely OR blocked_until > %(as_of)s)"
    )


class ProtocolProviderQuotaReader(Protocol):
    """Reads the active quota blocks of one tenant as of one instant."""

    def read_active_blocks(
        self, *, tenant_id: UUID, as_of: datetime
    ) -> Sequence[ModelProviderQuotaBlock]: ...


class StaticProviderQuotaReader:
    """A reader over a fixed set of blocks (tests, offline replay)."""

    def __init__(self, blocks: Iterable[ModelProviderQuotaBlock] = ()) -> None:
        self._blocks = tuple(blocks)

    def read_active_blocks(
        self, *, tenant_id: UUID, as_of: datetime
    ) -> Sequence[ModelProviderQuotaBlock]:
        return tuple(b for b in self._blocks if b.active_at(as_of))


class PostgresProviderQuotaReader:
    """Read-only reader of ``public.provider_quota_state`` (psycopg2).

    One statement per read, inside a transaction that sets the tenant GUC the
    table's RLS policy checks, and filters ``tenant_id`` explicitly as well.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout: int = _CONNECT_TIMEOUT_SECONDS,
        relation: str = PROVIDER_QUOTA_STATE_RELATION,
    ) -> None:
        if not dsn:
            raise ValueError("PostgresProviderQuotaReader requires a non-empty DSN")
        if not _RELATION_PATTERN.match(relation):
            raise ValueError(f"unsafe relation identifier: {relation!r}")
        self._dsn = dsn
        self._connect_timeout = connect_timeout
        self._sql = active_blocks_sql(relation)
        self._conn: psycopg2.extensions.connection | None = None

    def _get_conn(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            from omnimarket.projection.postgres_read_database import (
                connect_read_only,
            )

            self._conn = connect_read_only(
                self._dsn, connect_timeout=self._connect_timeout
            )
        return self._conn

    def read_active_blocks(
        self, *, tenant_id: UUID, as_of: datetime
    ) -> Sequence[ModelProviderQuotaBlock]:
        import psycopg2.extras  # type: ignore[import-untyped]

        conn = self._get_conn()
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT set_config(%s, %s, true)", (TENANT_GUC, str(tenant_id))
                )
                cur.execute(self._sql, {"tenant_id": str(tenant_id), "as_of": as_of})
                rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
        return tuple(
            ModelProviderQuotaBlock(
                credential_ref=str(row["credential_ref"]),
                provider_id=str(row["provider_id"]),
                model_scope=str(row["model_scope"]),
                disposition=str(row["disposition"]),
                blocked_until=row["blocked_until"],
                blocked_indefinitely=bool(row["blocked_indefinitely"]),
                provider_code=row["last_provider_code"],
                reason=str(row["block_reason"] or ""),
            )
            for row in rows
        )

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None


def resolve_provider_quota_reader() -> ProtocolProviderQuotaReader | None:
    """The live reader, from the lane's projection DSN; ``None`` when unset.

    ``None`` is not a pass: :func:`read_provider_quota_snapshot` turns it into
    an UNKNOWN snapshot, which fails closed for metered providers.
    """
    dsn = os.environ.get(_ENV_DSN, "").strip()
    if not dsn:
        return None
    return PostgresProviderQuotaReader(dsn)


def read_provider_quota_snapshot(
    reader: ProtocolProviderQuotaReader | None,
    *,
    tenant_id: object,
    now: datetime | None = None,
) -> ModelProviderQuotaSnapshot:
    """Read one tenant's active blocks. Never raises: unknown fails closed."""
    as_of = now or datetime.now(UTC)
    try:
        tenant = resolve_quota_tenant(tenant_id)
    except Exception as exc:
        return ModelProviderQuotaSnapshot.unknown(
            as_of=as_of, reason=f"no tenant resolves for the quota read: {exc}"
        )
    if reader is None:
        return ModelProviderQuotaSnapshot.unknown(
            as_of=as_of,
            tenant_id=tenant,
            reason=f"no provider quota reader is configured ({_ENV_DSN} unset)",
        )
    try:
        blocks = tuple(reader.read_active_blocks(tenant_id=tenant, as_of=as_of))
    except Exception as exc:
        _logger.warning(
            "provider quota projection unreadable for tenant %s; metered "
            "providers fail closed: %s",
            tenant,
            exc,
        )
        return ModelProviderQuotaSnapshot.unknown(
            as_of=as_of,
            tenant_id=tenant,
            reason=f"{type(exc).__name__}: {exc}",
        )
    return ModelProviderQuotaSnapshot(
        tenant_id=tenant, as_of=as_of, readable=True, blocks=blocks
    )


__all__ = [
    "PROVIDER_QUOTA_STATE_RELATION",
    "UNKNOWN_QUOTA_STATE_DISPOSITION",
    "ModelProviderQuotaBlock",
    "ModelProviderQuotaSnapshot",
    "PostgresProviderQuotaReader",
    "ProtocolProviderQuotaReader",
    "StaticProviderQuotaReader",
    "active_blocks_sql",
    "endpoint_is_metered",
    "quota_block_for_backend",
    "quota_domain_for_endpoint",
    "read_provider_quota_snapshot",
    "resolve_provider_quota_reader",
    "resolve_quota_tenant",
]
