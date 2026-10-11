# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The statement interface the writer stores through, and its SQLite store.

The writer's SQL is the Postgres subset SQLite shares: ``$n`` parameters,
``INSERT ... ON CONFLICT DO UPDATE`` and ``RETURNING``. Postgres is served by
the runtime's ``AsyncpgAdapter``, which already has this shape. The SQLite
store serves the local profile's file, the one ``SqliteDatabaseAdapter``
creates and the projection read node reads, so a row the writer stored there
is the row a typed read returns.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

StoreValue = str | int | float | bool | datetime | None


class ProtocolLivenessStore(Protocol):
    """Connect, run statements that return their rows, close."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def execute(
        self, query: str, *params: StoreValue
    ) -> list[dict[str, object]]: ...


def _sqlite_value(value: StoreValue) -> str | int | float | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, bool):
        return int(value)
    return value


class SqliteLivenessStore:
    """The local store file, written one transaction per statement.

    Every statement commits before it returns, so a writer that stops after a
    statement has lost nothing it reported as stored.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    async def connect(self) -> None:
        # Opening the shared adapter creates the three relations when absent.
        SqliteDatabaseAdapter(self._db_path).query("automation_liveness_state", limit=1)
        self._conn = sqlite3.connect(  # no-contract-check: projection boundary
            str(self._db_path)
        )
        self._conn.row_factory = sqlite3.Row

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def execute(self, query: str, *params: StoreValue) -> list[dict[str, object]]:
        if self._conn is None:
            raise RuntimeError("call connect() first")
        bound = {str(i): _sqlite_value(p) for i, p in enumerate(params, 1)}
        with self._conn:
            rows = self._conn.execute(query, bound).fetchall()
        return [dict(row) for row in rows]


__all__ = ["ProtocolLivenessStore", "SqliteLivenessStore", "StoreValue"]
