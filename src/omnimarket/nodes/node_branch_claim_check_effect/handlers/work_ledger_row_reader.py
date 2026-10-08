# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only, lazy Postgres work ledger boundary."""

from __future__ import annotations

import contextlib
import os
from datetime import datetime
from typing import Any, Protocol

from omnimarket.nodes.node_branch_claim_check_effect.handlers.branch_claim_resolution import (
    WorkLedgerRow,
)
from omnimarket.nodes.node_branch_claim_check_effect.models.model_branch_claim_policy import (
    ModelLedgerSource,
)


class ProtocolWorkLedgerRowReader(Protocol):
    def read_rows(
        self, *, ticket: str, since: datetime, until: datetime
    ) -> tuple[WorkLedgerRow, ...]: ...
    def read_row_ids(self, *, since: datetime, until: datetime) -> frozenset[str]: ...
    def newest_row_ts(self, *, until: datetime) -> datetime | None: ...


def format_database_error(exc: Exception, source: ModelLedgerSource) -> str:
    """Keep a database exception useful without exposing its connection string."""
    message = str(exc)
    dsn = os.environ.get(source.dsn_env)
    if dsn:
        message = message.replace(dsn, "[redacted]")
    if "postgres://" in message or "postgresql://" in message or "password=" in message:
        message = "database connection/query failed (connection details redacted)"
    return f"{type(exc).__name__}: {message}"


class PostgresWorkLedgerRowReader:
    def __init__(self, source: ModelLedgerSource) -> None:
        self._source = source
        self._conn: Any | None = None

    def _connect(self) -> Any:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        dsn = os.environ.get(self._source.dsn_env)
        if not dsn or not dsn.strip():
            raise RuntimeError(f"contract-declared {self._source.dsn_env} is unset")
        from omnimarket.projection.postgres_read_database import connect_read_only

        self._conn = connect_read_only(dsn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            conn.close()

    def _query(self, sql: str, params: tuple[object, ...]) -> list[tuple[Any, ...]]:
        try:
            with self._connect().cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.close()
            raise RuntimeError(format_database_error(exc, self._source)) from None

    def read_rows(
        self, *, ticket: str, since: datetime, until: datetime
    ) -> tuple[WorkLedgerRow, ...]:
        pattern = (
            "%"
            + ticket.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        rows = self._query(
            f"SELECT row_id, row_ts, raw_row FROM {self._source.relation} WHERE row_ts >= %s AND row_ts <= %s AND raw_row LIKE %s ORDER BY row_ts, row_id",
            (since, until, pattern),
        )
        return tuple((row_id, row_ts, raw_row) for row_id, row_ts, raw_row in rows)

    def read_row_ids(self, *, since: datetime, until: datetime) -> frozenset[str]:
        rows = self._query(
            f"SELECT row_id FROM {self._source.relation} WHERE row_ts >= %s AND row_ts <= %s",
            (since, until),
        )
        return frozenset(row[0] for row in rows)

    def newest_row_ts(self, *, until: datetime) -> datetime | None:
        rows = self._query(
            f"SELECT max(row_ts) FROM {self._source.relation} WHERE row_ts <= %s",
            (until,),
        )
        value = rows[0][0]
        if value is not None and not isinstance(value, datetime):
            raise TypeError("database newest row_ts is not a timestamp")
        return value
