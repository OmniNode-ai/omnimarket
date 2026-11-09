# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant-scoped sync Postgres read behind the delegation eval sources (OMN-20577).

Every read opens one read-only transaction, sets ``app.tenant_id`` with the
parameterised ``set_config(..., is_local => true)`` form the writer side uses, and only
then issues the statement, so the RLS predicate of the relation compares against the
tenant this reader was bound to. With the GUC unset an RLS-covered SELECT returns zero
rows and raises nothing (OMN-15797); so the tenant is resolved BEFORE any connection and
an unresolvable tenant raises ``TenantContextMissingError`` instead of reading an empty
table. The DSN is supplied by the caller (the sources read it from the node overlay),
with no default.
"""

from __future__ import annotations

import psycopg2  # type: ignore[import-untyped]
import psycopg2.extras  # type: ignore[import-untyped]

from omnimarket.projection.tenant_isolation import (
    TENANT_GUC,
    resolve_rls_read_tenant,
)

_CONNECT_TIMEOUT_SECONDS = 5


class TenantScopedRowReader:
    """``ProtocolTenantRowFetch`` over one DSN, bound to one tenant."""

    def __init__(
        self,
        dsn: str,
        tenant_id: str,
        *,
        connect_timeout: int = _CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        if not dsn:
            raise ValueError("TenantScopedRowReader requires a non-empty DSN")
        if not tenant_id.strip():
            raise ValueError("TenantScopedRowReader requires a tenant id")
        self._dsn = dsn
        self._tenant_id = tenant_id.strip()
        self._connect_timeout = connect_timeout

    def fetch(
        self, table: str, sql: str, params: tuple[object, ...]
    ) -> list[dict[str, object]]:
        # Resolved FIRST: a refused read issues no statement and opens no connection.
        tenant = resolve_rls_read_tenant(self._tenant_id, table=table)
        conn = psycopg2.connect(  # no-contract-check: read-only tenant-scoped eval source; DSN-injected
            self._dsn, connect_timeout=self._connect_timeout
        )
        try:
            conn.set_session(readonly=True, autocommit=False)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, tenant))
                cur.execute(sql, params)
                rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
            return rows
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
