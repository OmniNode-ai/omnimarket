"""PostgresDataSource — real DataSource adapter for .201 Postgres.

Connects to the omnidash_analytics database on .201:5436 and implements
the DataSource protocol for HandlerDataVerification.

Environment variables:
  OMNIDASH_ANALYTICS_DB_URL — full connection string
  OR individual: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

Usage:
  from omnimarket.nodes.node_data_verification.handlers.datasource_postgres import (
      PostgresDataSource,
  )
  ds = PostgresDataSource.from_env()
  result = handler.verify(command, ds)
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

try:
    import psycopg2  # type: ignore[import-untyped]

    _HAS_PSYCOPG2 = True
except ImportError:
    _HAS_PSYCOPG2 = False


class PostgresDataSource:
    """Real Postgres data source for production verification runs.

    Uses psycopg2 for synchronous queries. Suitable for short verification
    queries against the omnidash_analytics read-model DB.
    """

    def __init__(
        self,
        host: str = "192.168.86.201",
        port: int = 5436,
        database: str = "omnidash_analytics",
        user: str = "postgres",
        password: str = "",
    ) -> None:
        if not _HAS_PSYCOPG2:
            msg = (
                "psycopg2 is required for PostgresDataSource. "
                "Install with: uv add psycopg2-binary"
            )
            raise ImportError(msg)

        self._conn_params = {
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "password": password,
        }
        self._conn: Any = None

    @classmethod
    def from_env(cls) -> PostgresDataSource:
        """Create from environment variables."""
        db_url = os.environ.get("OMNIDASH_ANALYTICS_DB_URL")
        if db_url:
            parsed = urlparse(db_url)
            return cls(
                host=parsed.hostname or "192.168.86.201",
                port=parsed.port or 5436,
                database=parsed.path.lstrip("/") or "omnidash_analytics",
                user=parsed.username or "postgres",
                password=parsed.password or "",
            )
        return cls(
            host=os.environ.get("PGHOST", "192.168.86.201"),
            port=int(os.environ.get("PGPORT", "5436")),
            database=os.environ.get("PGDATABASE", "omnidash_analytics"),
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD", ""),
        )

    def _get_conn(self) -> Any:
        """Get or create a database connection."""
        if self._conn is None:
            self._conn = psycopg2.connect(**self._conn_params)
        return self._conn

    def get_row_count(self, table_name: str) -> int:
        """Get total row count for a table."""
        conn = self._get_conn()
        # Use identifier quoting to prevent SQL injection
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM %s" % _quote_ident(table_name)  # noqa: UP031
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def get_sample_rows(
        self, table_name: str, sample_size: int
    ) -> list[dict[str, str]]:
        """Get sample rows from a table."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM %s LIMIT %%s" % _quote_ident(table_name),  # noqa: UP031
                (sample_size,),
            )
            if cur.description is None:
                return []
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            return [
                {col: str(val) for col, val in zip(columns, row, strict=True)}
                for row in rows
            ]

    def get_columns(self, table_name: str) -> list[str]:
        """Get column names for a table."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s ORDER BY ordinal_position",
                (table_name,),
            )
            return [row[0] for row in cur.fetchall()]

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier to prevent injection.

    Only allows alphanumeric + underscore identifiers.
    """
    if not all(c.isalnum() or c == "_" for c in name):
        msg = f"Invalid table name: {name}"
        raise ValueError(msg)
    return f'"{name}"'


__all__: list[str] = ["PostgresDataSource"]
