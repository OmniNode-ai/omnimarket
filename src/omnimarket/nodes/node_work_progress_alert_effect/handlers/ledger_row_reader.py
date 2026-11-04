# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only Postgres effect boundary: one query of work_ledger_rows."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_work_progress_alert_effect.models import ModelLedgerRow

# Relation and row types are fixed by the projection, not deployment settings.
_QUERY = (
    "SELECT row_id, row_ts, row_type, row_lane, raw_row "
    "FROM omninode_internal.work_ledger_rows "
    "WHERE row_type IN ('CLAIM', 'STATUS', 'TERMINAL') "
    "AND row_lane IN ("
    "SELECT row_lane FROM omninode_internal.work_ledger_rows "
    "WHERE row_type = 'CLAIM' AND row_lane IS NOT NULL AND row_ts >= %s) "
    "ORDER BY row_lane, row_ts, row_id"
)


class LedgerSourceError(RuntimeError):
    """The ledger source is not configured or cannot be read."""


def resolve_dsn(dsn_env: str) -> str:
    """Use only the contract-named DSN; never guess a database identity."""
    dsn = os.environ.get(dsn_env, "")
    if not dsn.strip():
        raise LedgerSourceError(f"{dsn_env} is unset or empty")
    return dsn


class PostgresLedgerRowReader:
    """One read-only connection, opened on first use and dropped after an error."""

    def __init__(self, *, dsn: str) -> None:
        if not dsn.strip():
            raise LedgerSourceError("ledger reads DSN is empty")
        self._dsn = dsn
        self._conn: Any | None = None

    def _connect(self) -> Any:
        if self._conn is not None and getattr(self._conn, "closed", 0) == 0:
            return self._conn
        from omnimarket.projection.postgres_read_database import connect_read_only

        self._conn = connect_read_only(self._dsn)
        return self._conn

    def close(self) -> None:
        """Drop a failed connection before the next evaluation reads."""
        if self._conn is not None:
            connection, self._conn = self._conn, None
            connection.close()

    def read(self, claimed_since: datetime) -> Sequence[ModelLedgerRow]:
        try:
            with self._connect().cursor() as cursor:
                cursor.execute(_QUERY, (claimed_since,))
                fetched = cursor.fetchall()
            return tuple(
                ModelLedgerRow(
                    row_id=row[0],
                    row_ts=row[1],
                    row_type=row[2],
                    row_lane=row[3],
                    raw_row=row[4],
                )
                for row in fetched
            )
        except Exception:
            self.close()
            raise
