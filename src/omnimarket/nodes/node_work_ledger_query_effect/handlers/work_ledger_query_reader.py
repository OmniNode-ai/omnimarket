# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only, lazy Postgres work ledger query boundary."""

from __future__ import annotations

import contextlib
import importlib
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_ledger_query import ModelWorkLedgerRowRecord

# psycopg2 ships no type stubs; bind its sql module as Any.
pgsql: Any = importlib.import_module("psycopg2.sql")


class ModelWorkLedgerQuerySource(BaseModel):
    """Required contract configuration for the database ledger reader."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    dsn_env: str = Field(min_length=1)
    parity_receipt_lane: str = Field(min_length=1)
    binding_ref: str = Field(min_length=1)


def load_work_ledger_query_source(contract_path: Path) -> ModelWorkLedgerQuerySource:
    """Load and validate the contract's declared ledger source."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("work_ledger_query"), dict):
        raise ValueError("work_ledger_query.ledger_source must be a mapping")
    source = raw["work_ledger_query"].get("ledger_source")
    if not isinstance(source, dict):
        raise ValueError("work_ledger_query.ledger_source must be a mapping")
    return ModelWorkLedgerQuerySource.model_validate(source)


class WorkLedgerReadError(RuntimeError):
    """A failed database read, its message already redacted and type-prefixed."""


class ProtocolWorkLedgerQueryReader(Protocol):
    """Read rows, daily parity receipts and projection freshness."""

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]: ...
    def read_parity_receipts(
        self, *, since: datetime, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]: ...
    def freshness(
        self, *, until: datetime
    ) -> tuple[datetime | None, datetime | None]: ...


def format_database_error(exc: Exception, source: ModelWorkLedgerQuerySource) -> str:
    """Keep a database exception useful without exposing its connection string."""
    message = str(exc)
    dsn = os.environ.get(source.dsn_env)
    if dsn:
        message = message.replace(dsn, "[redacted]")
    if "postgres://" in message or "postgresql://" in message or "password=" in message:
        message = "database connection/query failed (connection details redacted)"
    return f"{type(exc).__name__}: {message}"


class PostgresWorkLedgerQueryReader:
    """Read the contract-declared relation through a lazy read-only connection."""

    def __init__(self, source: ModelWorkLedgerQuerySource) -> None:
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

    def _query(
        self, template: str, params: tuple[object, ...]
    ) -> list[tuple[Any, ...]]:
        """Run ``template`` with the relation bound as a quoted identifier."""
        schema, table = self._source.relation.split(".", 1)
        query = pgsql.SQL(template).format(relation=pgsql.Identifier(schema, table))
        try:
            with self._connect().cursor() as cursor:
                cursor.execute(query, params)
                return list(cursor.fetchall())
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.close()
            raise WorkLedgerReadError(
                format_database_error(exc, self._source)
            ) from None

    @staticmethod
    def _records(rows: list[tuple[Any, ...]]) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return tuple(
            ModelWorkLedgerRowRecord(
                row_id=row_id,
                row_ts=row_ts,
                row_type=row_type,
                row_lane=row_lane,
                text=raw_row.split("\n", 1)[0],
                raw_row=raw_row,
                source=source,
                projected_at=projected_at,
            )
            for row_id, row_ts, row_type, row_lane, raw_row, source, projected_at in rows
        )

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        if since is None:
            rows = self._query(
                "SELECT row_id, row_ts, row_type, row_lane, raw_row, source, projected_at FROM {relation} WHERE row_ts <= %s ORDER BY row_ts, projected_at, row_id",
                (until,),
            )
        else:
            rows = self._query(
                "SELECT row_id, row_ts, row_type, row_lane, raw_row, source, projected_at FROM {relation} WHERE row_ts >= %s AND row_ts <= %s ORDER BY row_ts, projected_at, row_id",
                (since, until),
            )
        return self._records(rows)

    def read_parity_receipts(
        self, *, since: datetime, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        rows = self._query(
            "SELECT row_id, row_ts, row_type, row_lane, raw_row, source, projected_at FROM {relation} WHERE row_type = 'STATUS' AND row_lane = %s AND row_ts >= %s AND row_ts <= %s ORDER BY row_ts, projected_at, row_id",
            (self._source.parity_receipt_lane, since, until),
        )
        return self._records(rows)

    def freshness(self, *, until: datetime) -> tuple[datetime | None, datetime | None]:
        rows = self._query(
            "SELECT max(row_ts), max(projected_at) FROM {relation} WHERE row_ts <= %s",
            (until,),
        )
        row_ts, projected_at = rows[0]
        if row_ts is not None and not isinstance(row_ts, datetime):
            raise TypeError("database newest row_ts is not a timestamp")
        if projected_at is not None and not isinstance(projected_at, datetime):
            raise TypeError("database newest projected_at is not a timestamp")
        return row_ts, projected_at
