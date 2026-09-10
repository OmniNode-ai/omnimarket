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
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from omnimarket.projection.upsert_statement import (
    SQL_EXPRESSION_SENTINEL_PREFIX,
    build_upsert_plan,
)

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
        self.upsert_returning(table, conflict_key, row)
        return True

    def upsert_returning(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
        *,
        tenant: str | None = None,
        insert_only_columns: frozenset[str] = frozenset(),
        sql_expression_columns: Mapping[str, str] = MappingProxyType({}),
        returning: Sequence[str] = (),
    ) -> list[dict[str, object]]:
        """OMN-18159. The local CLI evidence target, with the same write contract.

        ``tenant`` is accepted and ignored: SQLite has no row-level security,
        so there is no GUC for it to bind. The parameter stays on the signature
        because the protocol declares it and a caller must not have to know
        which sync target it is writing to.

        ``CURRENT_USER`` is NOT a SQLite keyword, so an attestation column
        would be a syntax error rather than a stamp. SQLite has no notion of a
        connected principal to attest to in the first place -- this file is a
        local evidence side-target, not a governed store -- so an expression
        column is recorded through the same sentinel the in-memory double
        uses. A local row that claimed a writer identity would be a fabricated
        attestation, which is precisely what the column exists to refuse.
        """
        plan = build_upsert_plan(
            table=table,
            conflict_key=conflict_key,
            row=row,
            insert_only_columns=insert_only_columns,
            sql_expression_columns=sql_expression_columns,
            returning=returning,
        )
        stamped = {
            column: f"{SQL_EXPRESSION_SENTINEL_PREFIX}{expression}>"
            for column, expression in plan.expression_columns.items()
        }
        bound = {**row, **stamped}

        conn = self._connect()
        try:
            self._ensure_columns(conn, table, bound)
            # Rendered with the expression columns folded into the bound set,
            # because SQLite must bind them rather than evaluate them, and
            # WITHOUT a RETURNING clause: SQLite's RETURNING needs 3.35+, and
            # leaving its rows unread would hold the statement open across the
            # commit. The stored row is read back below instead.
            statement = build_upsert_plan(
                table=table,
                conflict_key=conflict_key,
                row=bound,
                insert_only_columns=insert_only_columns,
            ).render(dialect="qmark_named")
            params = {col: self._encode(col, bound[col]) for col in bound}
            conn.execute(statement, params)
            conn.commit()
        finally:
            conn.close()

        if not plan.returning:
            return []
        stored = self.query(table, {key: row[key] for key in plan.conflict_keys})
        if not stored:
            return []
        return [{column: stored[0].get(column) for column in plan.returning}]

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        # OMN-17888: same validation and same statement shape as the Postgres
        # adapters. A column name cannot be a bind parameter, so an ordering
        # column is gated as an identifier before it is interpolated.
        if order_by is None and descending:
            raise ValueError("descending requires an order_by column")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise ValueError(f"limit must be a positive int, got {limit!r}")
        conn = self._connect()
        try:
            existing = self._existing_columns(conn, table)
            if order_by is not None and order_by not in existing:
                raise ValueError(
                    f"Invalid order_by column {order_by!r} for table {table!r}"
                )
            suffix = ""
            if order_by is not None:
                suffix += f" ORDER BY {order_by} {'DESC' if descending else 'ASC'}"
            if limit is not None:
                suffix += f" LIMIT {int(limit)}"
            if filters:
                clauses = [f"{key} = :{key}" for key in filters if key in existing]
                if not clauses:
                    return []
                where = " AND ".join(clauses)
                params = {key: self._encode(key, filters[key]) for key in filters}
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE {where}{suffix}",
                    params,
                ).fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM {table}{suffix}").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


__all__ = [
    "SqliteDatabaseAdapter",
    "default_evidence_db_path",
]
