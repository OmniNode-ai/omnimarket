# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Done-items store port.

One ledger event can close several tickets; ``apply_rows`` writes all of an
event's rows in one transaction, so a write that fails part-way leaves the
table as it was and the Done figure never counts half an event.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

StoreValue = str | int | float | bool | datetime | None


class ProtocolDoneItemsStore(Protocol):
    async def connect(self) -> None:
        """Open the store's connection."""

    async def close(self) -> None:
        """Close the store's connection."""

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]:
        """Run one statement and return its rows."""

    async def apply_rows(
        self, statement: str, rows: list[tuple[object, ...]]
    ) -> list[dict[str, object]]:
        """Run ``statement`` once per row in one transaction; return the rows it returned."""


def _sqlite_binding(params: tuple[object, ...]) -> dict[str, object]:
    converted: list[object] = []
    for param in params:
        if isinstance(param, datetime):
            converted.append(param.astimezone(UTC).isoformat())
        elif isinstance(param, bool):
            converted.append(int(param))
        else:
            converted.append(param)
    return {str(index): value for index, value in enumerate(converted, start=1)}


class SqliteDoneItemsStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    async def connect(self) -> None:
        self._conn = sqlite3.connect(  # no-contract-check: projection boundary
            str(self._db_path)
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS done_items (ticket TEXT NOT NULL PRIMARY KEY, done_at TEXT NOT NULL, done_row_id TEXT NOT NULL, lane TEXT, pr TEXT, closes TEXT, projected_at TEXT NOT NULL)"
        )
        self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connected(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteDoneItemsStore is not connected")
        return self._conn

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]:
        conn = self._connected()
        with conn:
            rows = conn.execute(query, _sqlite_binding(params)).fetchall()
        return [dict(row) for row in rows]

    async def apply_rows(
        self, statement: str, rows: list[tuple[object, ...]]
    ) -> list[dict[str, object]]:
        conn = self._connected()
        conn.execute("BEGIN IMMEDIATE")
        try:
            returned: list[dict[str, object]] = []
            for row in rows:
                returned.extend(
                    dict(r)
                    for r in conn.execute(statement, _sqlite_binding(row)).fetchall()
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return returned


class PostgresDoneItemsStore:
    """The runtime's store: the shared asyncpg pool, one transaction per event."""

    def __init__(self, adapter: AsyncpgAdapter) -> None:
        self._adapter = adapter

    async def connect(self) -> None:
        await self._adapter.connect()

    async def close(self) -> None:
        await self._adapter.close()

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]:
        rows: list[dict[str, Any]] = await self._adapter.execute(query, *params)
        return rows

    async def apply_rows(
        self, statement: str, rows: list[tuple[object, ...]]
    ) -> list[dict[str, object]]:
        returned: list[dict[str, object]] = []
        async with self._adapter.pool.acquire() as conn, conn.transaction():
            for row in rows:
                returned.extend(dict(r) for r in await conn.fetch(statement, *row))
        return returned


__all__ = [
    "PostgresDoneItemsStore",
    "ProtocolDoneItemsStore",
    "SqliteDoneItemsStore",
    "StoreValue",
]
