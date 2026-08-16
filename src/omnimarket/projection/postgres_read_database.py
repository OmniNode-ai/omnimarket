# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only sync Postgres projection adapter (OMN-14001 live ROI wiring).

A minimal ``ProtocolProjectionDatabaseSync`` implementation backed by a real
PostgreSQL connection, used to read materialised projection tables (e.g.
``context_roi_scores`` in ``omnidash_analytics``) back into a live decision. It
implements only the read half of the protocol — ``upsert`` raises, because this
adapter is deliberately read-only (the projection *writer* is
``node_projection_context_roi`` on the runtime bus, never this reader).

Connection is LAZY (established on first ``query``) with a bounded connect
timeout. Consumers of this adapter are fail-OPEN for TELEMETRY OUTAGES: a
missing DSN, unreachable host, or driver error degrades to static behaviour
rather than breaking the caller. See ``resolve_context_roi_db`` in
``omnimarket.routing.roi_overlay`` for the factory that gates construction on
the DSN env var.

Tenant seam (OMN-16092) -- fail-CLOSED, and deliberately NOT covered by that
fail-open posture:
    ``context_roi_scores`` is RLS-covered (``node_projection_context_roi``
    migration ``003_context_roi_scores_tenant_id_and_rls.sql``, policy
    ``tenant_isolation``: ``USING (tenant_id = current_setting('app.tenant_id',
    true))``). With the GUC unset the predicate is NULL and the ``SELECT``
    returns ZERO ROWS -- it does not error. A reader with no tenant seam is
    therefore blinded SILENTLY, and a fail-open consumer degrades with no
    signal distinguishable from "no data yet".

    So every read here opens a transaction, sets ``app.tenant_id`` with the
    same parameterized ``set_config(..., is_local => true)`` form the writer
    side uses (``PostgresSyncProjectionAdapter._tenant_scoped``, OMN-15306, and
    ``AsyncpgAdapter._set_tenant_context``), and only then issues the SELECT --
    so both sides of the policy agree. When no tenant can be resolved the read
    raises ``TenantContextMissingError`` BEFORE any SQL: a blinded read is a
    correctness defect, not an outage, and must never masquerade as one.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

import psycopg2  # type: ignore[import-untyped]
import psycopg2.extras  # type: ignore[import-untyped]

from omnimarket.projection.tenant_isolation import (
    TENANT_GUC,
    resolve_rls_read_tenant,
)

logger = logging.getLogger(__name__)

# Bounded connect timeout (seconds) so a routing read against an unreachable
# projection host cannot hang the caller past this ceiling before failing open.
_CONNECT_TIMEOUT_SECONDS = 3


class PostgresReadDatabaseAdapter:
    """Read-only ``ProtocolProjectionDatabaseSync`` over a Postgres DSN.

    Satisfies the ``query`` half of the sync projection-database protocol so a
    routing-time ROI read can consult a materialised projection table. ``upsert``
    is unsupported by design — this adapter never writes.

    ``tenant_id`` (OMN-16092) is the tenant every read runs under. ``None`` (the
    default) defers to :func:`resolve_rls_read_tenant`, which resolves the lane's
    configured tenant and falls back to the house tenant only while
    ``ENFORCE_TENANT_ISOLATION`` is off. A per-call ``filters["tenant_id"]``
    overrides it, mirroring ``PostgresSyncProjectionAdapter.query``.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout: int = _CONNECT_TIMEOUT_SECONDS,
        tenant_id: str | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("PostgresReadDatabaseAdapter requires a non-empty DSN")
        self._dsn = dsn
        self._connect_timeout = connect_timeout
        self._tenant_id = tenant_id
        self._conn: psycopg2.extensions.connection | None = None

    def _get_conn(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(  # no-contract-check: read-only projection boundary; DSN-injected fail-open reader
                self._dsn, connect_timeout=self._connect_timeout
            )
            # Read-only session — never mutate the projection table from the reader.
            self._conn.set_session(readonly=True, autocommit=True)
            logger.info(
                "PostgresReadDatabaseAdapter connected to %s",
                self._dsn.split("@")[-1],
            )
        return self._conn

    @staticmethod
    def _quote_ident(name: str) -> str:
        """Quote a SQL identifier, rejecting anything but a simple table name."""
        if not name.replace("_", "").isalnum():
            raise ValueError(f"unsafe table identifier: {name!r}")
        return f'"{name}"'

    def _resolve_tenant(self, table: str, filters: dict[str, object] | None) -> str:
        """Resolve the tenant this read runs under — raises before any SQL.

        Precedence: an explicit per-call ``filters["tenant_id"]``, then the
        adapter's constructor-injected tenant, then the lane's configured tenant
        / house-tenant fallback resolved by :func:`resolve_rls_read_tenant`.
        """
        explicit = (filters or {}).get("tenant_id")
        if not (isinstance(explicit, str) and explicit.strip()):
            explicit = self._tenant_id
        return resolve_rls_read_tenant(explicit, table=table)

    @staticmethod
    @contextlib.contextmanager
    def _tenant_scoped(
        conn: psycopg2.extensions.connection, tenant: str
    ) -> Iterator[None]:
        """Run the enclosed read in one tenant-scoped transaction (OMN-16092).

        ``set_config(..., is_local => true)`` is the parameterized,
        transaction-scoped form of ``SET LOCAL`` (``SET LOCAL`` itself takes no
        bind parameter). Autocommit — which this adapter otherwise keeps on — is
        dropped only for the duration of the read and always restored: under
        autocommit each statement is its own implicit transaction, so the GUC
        would evaporate before the SELECT ran and the RLS predicate would be
        NULL again. That silent no-op is the exact shape OMN-15306 fixed on the
        writer side; the same mechanism is used here so both agree.

        The transaction is committed (not left open) so the next read starts
        from a clean session and one tenant's context can never leak into it.
        """
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, tenant))
            yield
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        """Return rows from ``table`` (optionally filtered by equality) as dicts.

        The read runs inside a tenant-scoped transaction so RLS-covered tables
        (``context_roi_scores``) return the tenant's real rows instead of the
        silent empty set an unset ``app.tenant_id`` GUC produces. An unresolvable
        tenant raises :class:`TenantContextMissingError` before any connection is
        opened — fail-closed, never a plausible-looking empty result.
        """
        quoted = self._quote_ident(table)
        # Resolved FIRST, before any connection or SQL, so a refused read issues
        # no statement and cannot be mistaken for an empty table.
        tenant = self._resolve_tenant(table, filters)
        conn = self._get_conn()
        with (
            self._tenant_scoped(conn, tenant),
            conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur,
        ):
            if filters:
                cols = [c for c in filters if c.replace("_", "").isalnum()]
                if not cols:
                    return []
                where = " AND ".join(f'"{c}" = %({c})s' for c in cols)
                cur.execute(
                    f"SELECT * FROM {quoted} WHERE {where}",
                    {c: filters[c] for c in cols},
                )
            else:
                cur.execute(f"SELECT * FROM {quoted}")
            return [dict(row) for row in cur.fetchall()]

    def upsert(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
    ) -> bool:
        """Refuse writes — this adapter is read-only by design.

        The projection is written only by ``node_projection_context_roi`` on the
        runtime bus; a routing-time reader must never mutate it. This raises an
        explicit error so a mistaken write fails loudly at the call site.
        """
        raise RuntimeError(
            f"PostgresReadDatabaseAdapter is read-only and cannot upsert into "
            f"{table!r} (conflict_key={conflict_key!r}); the projection node owns writes"
        )

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()


__all__ = ["PostgresReadDatabaseAdapter"]
