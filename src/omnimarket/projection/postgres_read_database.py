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
timeout, and every consumer of this adapter is fail-OPEN: a missing DSN,
unreachable host, or driver error must degrade to static behaviour, never break
the caller. See ``resolve_context_roi_db`` in ``omnimarket.routing.roi_overlay``
for the fail-open factory that gates construction on the DSN env var.
"""

from __future__ import annotations

import logging

import psycopg2  # type: ignore[import-untyped]
import psycopg2.extras  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# Bounded connect timeout (seconds) so a routing read against an unreachable
# projection host cannot hang the caller past this ceiling before failing open.
_CONNECT_TIMEOUT_SECONDS = 3


class PostgresReadDatabaseAdapter:
    """Read-only ``ProtocolProjectionDatabaseSync`` over a Postgres DSN.

    Satisfies the ``query`` half of the sync projection-database protocol so a
    routing-time ROI read can consult a materialised projection table. ``upsert``
    is unsupported by design — this adapter never writes.
    """

    def __init__(
        self, dsn: str, *, connect_timeout: int = _CONNECT_TIMEOUT_SECONDS
    ) -> None:
        if not dsn:
            raise ValueError("PostgresReadDatabaseAdapter requires a non-empty DSN")
        self._dsn = dsn
        self._connect_timeout = connect_timeout
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

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        """Return rows from ``table`` (optionally filtered by equality) as dicts."""
        quoted = self._quote_ident(table)
        conn = self._get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
