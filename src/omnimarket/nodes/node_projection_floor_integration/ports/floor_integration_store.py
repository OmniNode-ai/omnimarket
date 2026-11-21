# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Floor integration rows store port.

One integration pass is applied in one transaction (``apply_pass``): the held
pass is read from the floor_integration_pass marker, and unless the held pass is
newer, or is the same pass with the same content, the pass's rows are upserted,
the rows it no longer names are removed, and the marker records its as_of, all
committed together or not at all. A failure part-way through leaves the held
pass exactly as it was, so the served Running figure never mixes two passes; the
marker keeps an empty pass held, so a late older pass changes nothing. Limit: a
delta whose publish fails after the pass committed is not published again.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

StoreValue = str | int | float | bool | datetime | None


@dataclass(frozen=True)
class PassOutcome:
    """What one applied pass changed: nothing when the held pass is current."""

    skipped: bool
    written_keys: list[str] = field(default_factory=list)
    deleted_keys: list[str] = field(default_factory=list)


class ProtocolFloorIntegrationStore(Protocol):
    async def connect(self) -> None:
        """Open the store's connection."""

    async def close(self) -> None:
        """Close the store's connection."""

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]:
        """Run one statement and return its rows."""

    async def apply_pass(
        self,
        *,
        lock_key: str,
        held_statement: str,
        upsert_statement: str,
        prune_statement: str,
        incoming_as_of: str,
        rows: list[tuple[object, ...]],
        incoming_keys: set[str],
        held_is_newer: Callable[[object], bool],
        held_is_same_pass: Callable[[object], bool],
        held_rows_statement: str,
        unchanged: Callable[[list[dict[str, object]]], bool],
        delete_key_statement: str,
        marker_statement: str,
    ) -> PassOutcome:
        """Apply one pass in one transaction.

        Skipped when the held pass is newer, or is the same pass (same as_of) with
        the same content. A same-as_of pass with corrected content is applied,
        and held keys it no longer names are deleted.
        """


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
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS floor_integration_pass ("
            "id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1), as_of TEXT NOT NULL, "
            "projected_at TEXT NOT NULL)"
        )
        self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connected(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteFloorIntegrationStore is not connected")
        return self._conn

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]:
        conn = self._connected()
        with conn:
            rows = conn.execute(query, _sqlite_binding(params)).fetchall()
        return [dict(row) for row in rows]

    async def apply_pass(
        self,
        *,
        lock_key: str,
        held_statement: str,
        upsert_statement: str,
        prune_statement: str,
        incoming_as_of: str,
        rows: list[tuple[object, ...]],
        incoming_keys: set[str],
        held_is_newer: Callable[[object], bool],
        held_is_same_pass: Callable[[object], bool],
        held_rows_statement: str,
        unchanged: Callable[[list[dict[str, object]]], bool],
        delete_key_statement: str,
        marker_statement: str,
    ) -> PassOutcome:
        conn = self._connected()
        # BEGIN IMMEDIATE takes the write lock before the held pass is read, so
        # a second writer cannot apply a pass between the read and the write.
        conn.execute("BEGIN IMMEDIATE")
        try:
            held = conn.execute(held_statement).fetchone()
            held_as_of = held["as_of"] if held is not None else None
            absent: list[str] = []
            if held_is_newer(held_as_of):
                conn.execute("ROLLBACK")
                return PassOutcome(skipped=True)
            if held_is_same_pass(held_as_of):
                held_rows = [
                    dict(r) for r in conn.execute(held_rows_statement).fetchall()
                ]
                if unchanged(held_rows):
                    conn.execute("ROLLBACK")
                    return PassOutcome(skipped=True)
                absent = sorted({str(r["key"]) for r in held_rows} - incoming_keys)
            written: list[str] = []
            for row in rows:
                returned = conn.execute(
                    upsert_statement, _sqlite_binding(row)
                ).fetchone()
                if returned is not None:
                    written.append(str(returned["key"]))
            deleted = [
                str(gone["key"])
                for gone in conn.execute(
                    prune_statement, _sqlite_binding((incoming_as_of,))
                ).fetchall()
            ]
            for key in absent:
                deleted.extend(
                    str(gone["key"])
                    for gone in conn.execute(
                        delete_key_statement, _sqlite_binding((key,))
                    ).fetchall()
                )
            conn.execute(marker_statement, _sqlite_binding((incoming_as_of,)))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return PassOutcome(skipped=False, written_keys=written, deleted_keys=deleted)


class PostgresFloorIntegrationStore:
    """The runtime's store: the shared asyncpg pool, one transaction per pass."""

    def __init__(self, adapter: AsyncpgAdapter) -> None:
        self._adapter = adapter

    async def connect(self) -> None:
        await self._adapter.connect()

    async def close(self) -> None:
        await self._adapter.close()

    async def execute(self, query: str, *params: object) -> list[dict[str, object]]:
        rows: list[dict[str, Any]] = await self._adapter.execute(query, *params)
        return rows

    async def apply_pass(
        self,
        *,
        lock_key: str,
        held_statement: str,
        upsert_statement: str,
        prune_statement: str,
        incoming_as_of: str,
        rows: list[tuple[object, ...]],
        incoming_keys: set[str],
        held_is_newer: Callable[[object], bool],
        held_is_same_pass: Callable[[object], bool],
        held_rows_statement: str,
        unchanged: Callable[[list[dict[str, object]]], bool],
        delete_key_statement: str,
        marker_statement: str,
    ) -> PassOutcome:
        # The pool is used directly so one pass is one transaction. That skips
        # AsyncpgAdapter.execute's tenant guard, which does not apply here:
        # floor_integration_rows has no tenant column. A tenant-scoped table needs it back.
        async with self._adapter.pool.acquire() as conn, conn.transaction():
            # One writer per table at a time, across processes: the held pass
            # is read under the lock, so two passes cannot interleave.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", lock_key)
            held = await conn.fetchrow(held_statement)
            held_as_of = held["as_of"] if held is not None else None
            absent: list[str] = []
            if held_is_newer(held_as_of):
                return PassOutcome(skipped=True)
            if held_is_same_pass(held_as_of):
                held_rows = [dict(r) for r in await conn.fetch(held_rows_statement)]
                if unchanged(held_rows):
                    return PassOutcome(skipped=True)
                absent = sorted({str(r["key"]) for r in held_rows} - incoming_keys)
            written: list[str] = []
            for row in rows:
                returned = await conn.fetchrow(upsert_statement, *row)
                if returned is not None:
                    written.append(str(returned["key"]))
            deleted = [
                str(gone["key"])
                for gone in await conn.fetch(prune_statement, incoming_as_of)
            ]
            for key in absent:
                deleted.extend(
                    str(gone["key"])
                    for gone in await conn.fetch(delete_key_statement, key)
                )
            await conn.execute(marker_statement, incoming_as_of)
        return PassOutcome(skipped=False, written_keys=written, deleted_keys=deleted)


__all__ = [
    "PassOutcome",
    "PostgresFloorIntegrationStore",
    "ProtocolFloorIntegrationStore",
    "SqliteFloorIntegrationStore",
    "StoreValue",
]
