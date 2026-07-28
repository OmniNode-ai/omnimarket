# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synchronous Postgres-backed ``DatabaseAdapter`` for in-process projections.

The deployed runtime projects delegation terminal events into Postgres via the
async projection runners (``BaseProjectionRunner`` + ``AsyncpgAdapter``). The
local ``onex delegate`` (standalone CLI, no broker) path runs the SAME canonical
projection handler in-process, but through the SYNC
``ProtocolProjectionDatabaseSync`` boundary. Until OMN-14015 the only sync
implementation was :class:`SqliteDatabaseAdapter`, so the bus-less CLI could only
ever write its evidence to a local SQLite side-target — never to the platform
Postgres substrate the dashboards and ``context_roi_scores`` are built from.

This adapter closes that gap: it implements ``ProtocolProjectionDatabaseSync``
over psycopg2 so the local delegation path can target the platform Postgres
substrate purely by configuration (a projection runtime binding overlay whose
``database_url`` is a Postgres DSN), with no code branch and no SQLite
side-target. It is the sync counterpart of ``AsyncpgAdapter`` for the CLI path.

Schema authority: unlike :class:`SqliteDatabaseAdapter` (which additively
``ALTER``s a throwaway local file), this adapter NEVER mutates the schema. The
migration set (``node_projection_delegation/migrations``) owns the Postgres
table. A row column that does not exist on the target surfaces a loud psycopg2
error (best-effort-swallowed by the caller's evidence write) rather than
silently mutating a shared, migration-governed table.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Iterator
from typing import Any

from omnimarket.projection.tenant_isolation import (
    TENANT_GUC,
    resolve_read_tenant,
    resolve_write_tenant,
)

logger = logging.getLogger(__name__)

# Strict identifier validation for table/column names composed into SQL. These
# come from trusted internal projection constants and typed row keys (never user
# input), but validating keeps the composed SQL provably injection-free — the
# same posture ``PostgresDataSource`` applies to its table names.
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(name: str, *, kind: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"invalid {kind} identifier: {name!r}")
    return name


class PostgresSyncProjectionAdapter:
    """``ProtocolProjectionDatabaseSync`` over a psycopg2 Postgres connection.

    UPSERT keys on the conflict column(s); JSON/list/dict values are adapted to
    JSONB via ``psycopg2.extras.Json``; the schema is never mutated (migrations
    own it). A fresh connection is opened per operation and closed afterwards —
    matching :class:`SqliteDatabaseAdapter`'s connect-per-call model, which is
    appropriate for the low-frequency, one-shot CLI evidence write this adapter
    serves (it is NOT a hot projection-runner loop, which uses ``AsyncpgAdapter``
    with a pool).
    """

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgresSyncProjectionAdapter requires a non-empty DSN")
        self._dsn = dsn

    def _connect(self) -> Any:
        # This IS the ProtocolProjectionDatabaseSync I/O boundary adapter (the
        # Postgres analog of the SQLite projection adapter that owns its own
        # connection); the connection is the adapter's purpose, not a
        # contract-bypassing freestanding call (OMN-13160/OMN-14015). The
        # no-contract-check tag is the scanner's sanctioned per-line boundary
        # annotation, NOT a path-allowlist broadening.
        import psycopg2  # type: ignore[import-untyped]

        conn = psycopg2.connect(self._dsn)  # no-contract-check: projection boundary
        conn.autocommit = True
        return conn

    @staticmethod
    @contextlib.contextmanager
    def _tenant_scoped(conn: Any, tenant: str) -> Iterator[None]:
        """Run the enclosed statements in one tenant-scoped transaction.

        OMN-15306. Tenant-scoped projection tables carry an RLS policy comparing
        ``tenant_id`` against the ``app.tenant_id`` GUC, so a write with no
        tenant context is rejected outright:
        ``new row violates row-level security policy``.

        ``set_config(..., is_local => true)`` is the parameterized,
        transaction-scoped form of ``SET LOCAL`` (``SET LOCAL`` itself takes no
        bind parameter, so the tenant would have to be interpolated into the SQL
        text). It is the same mechanism the onex-api reader seam uses, so both
        sides of the policy agree.

        Autocommit is dropped only for the duration of the statement and always
        restored: a bare ``SET LOCAL`` under autocommit is a no-op, because each
        statement becomes its own implicit transaction and the GUC evaporates
        before the INSERT runs.
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

    @staticmethod
    def _adapt(value: object) -> object:
        """Adapt a Python value for a psycopg2 parameter.

        list/dict -> JSONB via ``Json`` (the target columns, e.g.
        ``quality_gates_checked_jsonb`` / ``premium_counterfactual``, are JSONB).
        ``Decimal`` and ``bool`` bind natively through psycopg2, so they pass
        through unchanged (unlike the SQLite adapter, which must coerce them).
        """
        if isinstance(value, (list, dict)):
            from psycopg2.extras import Json  # type: ignore[import-untyped]

            return Json(value)
        return value

    def upsert(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
    ) -> bool:
        # OMN-15306: resolved before any connection or SQL, so an enforced
        # refusal cannot leave a partially-written row.
        tenant = resolve_write_tenant(row.get("tenant_id"), table=table)
        conflict_keys = [key.strip() for key in conflict_key.split(",") if key.strip()]
        if not conflict_keys:
            raise ValueError("conflict_key must contain at least one key")
        missing = [key for key in conflict_keys if key not in row]
        if missing:
            raise KeyError(f"row missing conflict key(s): {missing}")

        table_ident = _validate_identifier(table, kind="table")
        columns = [_validate_identifier(col, kind="column") for col in row]
        for key in conflict_keys:
            _validate_identifier(key, kind="conflict-key")

        placeholders = ", ".join(f"%({col})s" for col in columns)
        update_cols = [col for col in columns if col not in conflict_keys]
        set_clause = ", ".join(f"{col} = EXCLUDED.{col}" for col in update_cols)
        conflict_clause = ", ".join(conflict_keys)
        on_conflict = f"DO UPDATE SET {set_clause}" if update_cols else "DO NOTHING"
        statement = (
            f"INSERT INTO {table_ident} ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_clause}) {on_conflict}"
        )
        params = {col: self._adapt(row[col]) for col in columns}

        conn = self._connect()
        try:
            with self._tenant_scoped(conn, tenant), conn.cursor() as cur:
                cur.execute(statement, params)
            return True
        finally:
            conn.close()

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        from psycopg2.extras import RealDictCursor

        table_ident = _validate_identifier(table, kind="table")
        # OMN-15306: reads need the GUC too. Without it an RLS-covered table
        # returns ZERO rows rather than erroring, so existing-row probes would
        # silently conclude no prior row exists and clobber real evidence.
        tenant = resolve_read_tenant((filters or {}).get("tenant_id"))
        conn = self._connect()
        try:
            with (
                self._tenant_scoped(conn, tenant),
                conn.cursor(cursor_factory=RealDictCursor) as cur,
            ):
                if filters:
                    filter_cols = [
                        _validate_identifier(key, kind="filter-column")
                        for key in filters
                    ]
                    where = " AND ".join(f"{col} = %({col})s" for col in filter_cols)
                    params = {col: self._adapt(filters[col]) for col in filters}
                    cur.execute(
                        f"SELECT * FROM {table_ident} WHERE {where}",
                        params,
                    )
                else:
                    cur.execute(f"SELECT * FROM {table_ident}")
                return [dict(record) for record in cur.fetchall()]
        finally:
            conn.close()


__all__ = ["PostgresSyncProjectionAdapter"]
