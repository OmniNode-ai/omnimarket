# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Floor integration rows store port."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

StoreValue = str | int | float | bool | datetime | None


class ProtocolFloorIntegrationStore(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]: ...


class SqliteFloorIntegrationStore:
    """The local dashboard's store: the table is made on connect, timestamps are ISO text."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    async def connect(self) -> None:
        self._conn = sqlite3.connect(  # no-contract-check: projection boundary
            str(self._db_path)
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS floor_integration_rows ("
            "key TEXT NOT NULL PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL, "
            "title TEXT NOT NULL, merged_at TEXT NOT NULL, target TEXT NOT NULL, state TEXT NOT NULL, "
            "gap_class TEXT NOT NULL, step TEXT, running_path TEXT, running_line TEXT, "
            "merged INTEGER NOT NULL, seen_running INTEGER NOT NULL, settled_elsewhere INTEGER NOT NULL, "
            "reading_owed INTEGER NOT NULL, unintegrated INTEGER NOT NULL, as_of TEXT NOT NULL, "
            "projected_at TEXT NOT NULL)"
        )
        self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]:
        if self._conn is None:
            raise RuntimeError("SqliteFloorIntegrationStore is not connected")
        converted: list[object] = []
        for param in params:
            if isinstance(param, datetime):
                converted.append(param.astimezone(UTC).isoformat())
            elif isinstance(param, bool):
                converted.append(int(param))
            else:
                converted.append(param)
        binding = {str(index): value for index, value in enumerate(converted, start=1)}
        with self._conn:
            cursor = self._conn.execute(query, binding)
            rows = cursor.fetchall()
        return [dict(row) for row in rows]


__all__ = [
    "ProtocolFloorIntegrationStore",
    "SqliteFloorIntegrationStore",
    "StoreValue",
]
