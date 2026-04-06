"""PostgresDataSource — real DB adapter for data verification.

Implements the DataSource protocol using psycopg2 to query the
omnidash_analytics database on .201:5436.

Usage:
    from omnimarket.nodes.node_data_verification.handlers.datasource_postgres import (
        PostgresDataSource,
    )

    ds = PostgresDataSource()  # reads OMNIDASH_ANALYTICS_DB_URL from env
    ds = PostgresDataSource(dsn="postgresql://user:pass@host:5436/omnidash_analytics")
"""

from __future__ import annotations

import logging
import os
import re

import psycopg2  # type: ignore[import-untyped]
import psycopg2.extras  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# Strict table name validation: only allow alphanumeric + underscore
_TABLE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

DB_URL_ENV = "OMNIDASH_ANALYTICS_DB_URL"


class PostgresDataSource:
    """DataSource backed by a real PostgreSQL connection.

    Satisfies the DataSource protocol defined in handler_data_verification.py.
    Uses psycopg2 with RealDictCursor for dict-style row access.
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ.get(DB_URL_ENV, "")
        self._conn: psycopg2.extensions.connection | None = None
        if not self._dsn:
            raise RuntimeError(
                f"{DB_URL_ENV} not set and no DSN provided. "
                "Set the environment variable or pass dsn= explicitly."
            )

    def _get_conn(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = True
            logger.info("PostgresDataSource connected to %s", self._dsn.split("@")[-1])
        return self._conn

    @staticmethod
    def _quote_table(table_name: str) -> str:
        """Validate and quote a table name to prevent SQL injection."""
        if not _TABLE_NAME_RE.match(table_name):
            raise ValueError(f"Invalid table name: {table_name!r}")
        return f'"{table_name}"'

    def get_row_count(self, table_name: str) -> int:
        """Return total row count for the given table."""
        conn = self._get_conn()
        quoted = self._quote_table(table_name)
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {quoted}")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def get_sample_rows(
        self, table_name: str, sample_size: int
    ) -> list[dict[str, str]]:
        """Return up to sample_size rows, newest first.

        All values are stringified to match the DataSource protocol
        (handler checks operate on string representations).
        """
        conn = self._get_conn()
        quoted = self._quote_table(table_name)

        # Try ORDER BY created_at DESC for time-ordered tables,
        # fall back to unordered if column doesn't exist
        query = f"SELECT * FROM {quoted} ORDER BY created_at DESC LIMIT %s"
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, (sample_size,))
                rows = cur.fetchall()
        except psycopg2.errors.UndefinedColumn:
            conn.rollback()
            query_fallback = f"SELECT * FROM {quoted} LIMIT %s"
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query_fallback, (sample_size,))
                rows = cur.fetchall()

        # Stringify all values for the handler's string-based checks
        return [
            {str(k): str(v) if v is not None else "" for k, v in dict(row).items()}
            for row in rows
        ]

    def get_columns(self, table_name: str) -> list[str]:
        """Return column names for the given table from information_schema."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = %s
                  AND table_schema = 'public'
                ORDER BY ordinal_position
                """,
                (table_name,),
            )
            return [row[0] for row in cur.fetchall()]

    def close(self) -> None:
        """Close the database connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()
            logger.info("PostgresDataSource connection closed")

    def __del__(self) -> None:
        if hasattr(self, "_conn"):
            self.close()


__all__: list[str] = ["PostgresDataSource"]
