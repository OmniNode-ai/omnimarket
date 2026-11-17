# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Done-items store port."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

StoreValue = str | int | float | bool | datetime | None


class ProtocolDoneItemsStore(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]: ...


class SqliteDoneItemsStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    async def connect(self) -> None:
        self._conn = sqlite3.connect(
            str(self._db_path)
        )  # no-contract-check: projection boundary
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS done_items (ticket TEXT NOT NULL PRIMARY KEY, done_at TEXT NOT NULL, done_row_id TEXT NOT NULL, lane TEXT, pr TEXT, closes TEXT, projected_at TEXT NOT NULL)"
        )
        self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]:
        if self._conn is None:
            raise RuntimeError("SqliteDoneItemsStore is not connected")

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
