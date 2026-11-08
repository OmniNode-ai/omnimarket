# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A file-backed conditional store for the tests: a real store two processes can share.

The write is one ``UPDATE ... WHERE version = ?`` or one insert-if-absent, so a lost race is a refused write, not a
lock. The crash, race and process tests run against it.
"""

from __future__ import annotations

import sqlite3

from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ModelStateRead,
)

_TABLE = "floor_fill_state"


class SqliteFloorStateStore:
    """A file-backed conditional store: the write is one ``UPDATE ... WHERE version = ?`` or one insert-if-absent."""

    def __init__(self, path: str, *, busy_timeout_seconds: float) -> None:
        self._path = path
        self._busy_timeout = busy_timeout_seconds
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self._path, timeout=self._busy_timeout, isolation_level=None
        )

    def _ensure_table(self) -> None:
        try:
            with self._connect() as db:
                db.execute(
                    f"CREATE TABLE IF NOT EXISTS {_TABLE} "
                    "(scope TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL)"
                )
        except sqlite3.Error as error:
            raise FloorFillUnreadableError(f"store-unreachable:{error}") from error

    def load(self, key: str) -> ModelStateRead:
        try:
            db = self._connect()
            try:
                row = db.execute(
                    f"SELECT payload, version FROM {_TABLE} WHERE scope = ?", (key,)
                ).fetchone()
            finally:
                db.close()
        except sqlite3.Error as error:
            raise FloorFillUnreadableError(f"store-unreachable:{error}") from error
        if row is None:
            return ModelStateRead(payload=None, version=0)
        return ModelStateRead(payload=row[0], version=row[1])

    def compare_and_set(
        self, key: str, expected_version: int, new_version: int, payload: str
    ) -> bool:
        try:
            db = self._connect()
            try:
                if expected_version == 0:
                    cursor = db.execute(
                        f"INSERT INTO {_TABLE} (scope, version, payload) VALUES (?, ?, ?) "
                        "ON CONFLICT(scope) DO NOTHING",
                        (key, new_version, payload),
                    )
                else:
                    cursor = db.execute(
                        f"UPDATE {_TABLE} SET version = ?, payload = ? "
                        "WHERE scope = ? AND version = ?",
                        (new_version, payload, key, expected_version),
                    )
                return cursor.rowcount == 1
            finally:
                db.close()
        except sqlite3.Error as error:
            raise FloorFillUnreadableError(f"store-unreachable:{error}") from error


__all__: list[str] = ["SqliteFloorStateStore"]
