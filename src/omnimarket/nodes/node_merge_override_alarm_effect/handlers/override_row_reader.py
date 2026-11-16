# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only Postgres effect boundary: RULING rows of work_ledger_rows naming one pull request."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from omnimarket.nodes.node_merge_override_alarm_effect.models import ModelLedgerRow

# The head is deliberately not in the query: the pure matcher compares it.
_QUERY = (
    "SELECT row_id, row_ts, row_type, row_lane, raw_row "
    "FROM omninode_internal.work_ledger_rows "
    "WHERE row_type = 'RULING' AND raw_row LIKE %s AND raw_row LIKE %s "
    "ORDER BY row_ts, row_id"
)


class LedgerSourceError(RuntimeError):
    """The ledger source is not configured or cannot be read."""


def resolve_dsn(dsn_env: str) -> str:
    """Use only the contract-named DSN; never guess a database identity."""
    dsn = os.environ.get(dsn_env, "")
    if not dsn.strip():
        raise LedgerSourceError(f"{dsn_env} is unset or empty")
    return dsn


class PostgresOverrideRowReader:
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
        if self._conn is not None:
            connection, self._conn = self._conn, None
            connection.close()

    def read_override_rows(
        self, repository: str, pr_number: int
    ) -> Sequence[ModelLedgerRow]:
        name = repository.split("/", 1)[-1]
        try:
            with self._connect().cursor() as cursor:
                cursor.execute(_QUERY, ("%override=merge%", f"%{name}#{pr_number}%"))
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
