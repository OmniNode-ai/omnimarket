# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SQLite-backed ``DatabaseAdapter`` for in-process local-runtime projections.

The deployed runtime projects delegation terminal events into Postgres via the
async projection runners. The local ``onex delegate`` (standalone CLI) path runs
in-process with no broker and no Postgres, but it must STILL materialize a
``delegation_events`` evidence row so the local delegation tail is not silently
dropped (OMN-13160).

This adapter implements ``ProtocolProjectionDatabaseSync`` over SQLite so the
SAME canonical projection handler (``HandlerProjectionDelegation``) materializes
the local evidence row that the deprecated DirectCurl port's bespoke sqlite write
used to produce. It is NOT a substitute for the Postgres projection on the bus
runtime — it is the local in-process projection target only.

The schema is created idempotently. Columns are added additively as the
projection row dictates so the adapter never fails on an unknown column from a
newer projection version.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

_DEFAULT_EVIDENCE_DB_PATH = (
    Path.home() / ".omninode" / "delegation" / "delegation.sqlite"
)

# Base schema mirrors the deployed delegation_events projection target so a
# locally created DB matches the columns the projection handler writes. The
# correlation_id UNIQUE constraint backs the UPSERT dedup.
_DELEGATION_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS delegation_events (
    correlation_id          TEXT    NOT NULL UNIQUE
)
"""

# JSON-serialized columns: list/dict values are stored as TEXT JSON so the
# sqlite row round-trips structurally for evidence queries.
_JSON_COLUMNS = frozenset(
    {
        "quality_gates_checked_jsonb",
        "quality_gates_failed_jsonb",
    }
)


def default_evidence_db_path() -> Path:
    """Return the canonical local delegation evidence sqlite path."""
    return _DEFAULT_EVIDENCE_DB_PATH


class SqliteDatabaseAdapter:
    """``ProtocolProjectionDatabaseSync`` over a local SQLite file.

    Idempotent schema creation; additive columns inferred from the row dict so a
    newer projection row never breaks an older DB file. UPSERT keys on the
    conflict column(s).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # This IS the ProtocolProjectionDatabaseSync I/O boundary adapter (the SQLite
        # analog of the deployed Postgres projection adapter that owns its own
        # connection); the connection is the adapter's purpose, not a contract-bypassing
        # freestanding call (OMN-13160). The no-contract-check tag below is the scanner's
        # sanctioned per-line boundary annotation, NOT a path-allowlist broadening.
        db_path = str(self._db_path)
        conn = sqlite3.connect(db_path)  # no-contract-check: projection boundary
        conn.row_factory = sqlite3.Row
        conn.execute(_DELEGATION_EVENTS_DDL)
        conn.commit()
        return conn

    @staticmethod
    def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_columns(
        self, conn: sqlite3.Connection, table: str, row: dict[str, object]
    ) -> None:
        existing = self._existing_columns(conn, table)
        for column in row:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        conn.commit()

    @staticmethod
    def _encode(column: str, value: object) -> object:
        if column in _JSON_COLUMNS or isinstance(value, list | dict):
            return json.dumps(value)
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, Decimal):
            # sqlite cannot bind Decimal; store cost columns as float text-safe.
            return float(value)
        return value

    def upsert(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
    ) -> bool:
        conflict_keys = [key.strip() for key in conflict_key.split(",") if key.strip()]
        if not conflict_keys:
            raise ValueError("conflict_key must contain at least one key")
        missing = [key for key in conflict_keys if key not in row]
        if missing:
            raise KeyError(f"row missing conflict key(s): {missing}")

        conn = self._connect()
        try:
            self._ensure_columns(conn, table, row)
            columns = list(row.keys())
            placeholders = ", ".join(f":{col}" for col in columns)
            update_cols = [col for col in columns if col not in conflict_keys]
            set_clause = ", ".join(f"{col} = excluded.{col}" for col in update_cols)
            conflict_clause = ", ".join(conflict_keys)
            on_conflict = f"DO UPDATE SET {set_clause}" if update_cols else "DO NOTHING"
            params = {col: self._encode(col, row[col]) for col in columns}
            conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT({conflict_clause}) {on_conflict}",
                params,
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        conn = self._connect()
        try:
            existing = self._existing_columns(conn, table)
            if filters:
                clauses = [f"{key} = :{key}" for key in filters if key in existing]
                if not clauses:
                    return []
                where = " AND ".join(clauses)
                params = {key: self._encode(key, filters[key]) for key in filters}
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE {where}",
                    params,
                ).fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


__all__ = [
    "SqliteDatabaseAdapter",
    "default_evidence_db_path",
]
