# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21349: preserve correlation-keyed claims before creating delivery claims.

An older store has no delivery_id, so the pending index cannot be created over
it. The store step keeps those rows in a legacy table and creates the current
shape before either claims index is built, without inventing delivery ids.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

STEP = "omn21349_claims_delivery_id_shape"
TABLE = "delegate_skill_command_claims"
LEGACY_TABLE = "delegate_skill_command_claims_legacy_no_delivery_id"
PENDING_INDEX = "delegate_skill_command_claims_pending_idx"
CORRELATION_INDEX = "delegate_skill_command_claims_correlation_idx"
OLD_ROW = (
    "00000000-0000-4000-8000-000000000001",
    "tenant-a",
    "2026-10-23T12:00:00+00:00",
    "{}",
)
NEW_ROW = (
    "00000000-0000-4000-8000-000000000002",
    OLD_ROW[0],
    OLD_ROW[2],
    "",
)


@pytest.fixture
def old_shape_store(tmp_path: Path) -> Path:
    path = tmp_path / "claims.sqlite"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            f"CREATE TABLE {TABLE} ("
            "correlation_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL DEFAULT '', "
            "claimed_at TEXT NOT NULL, "
            "terminal_json TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?)", OLD_ROW)
        conn.commit()
    return path


def _legacy_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if str(row[0]).startswith(LEGACY_TABLE)
    }


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
            (table,),
        )
    }


def test_claims_table_old_shape_preserved_and_reopened(old_shape_store: Path) -> None:
    adapter = SqliteDatabaseAdapter(old_shape_store)
    with closing(adapter._connect()) as conn:
        columns = {
            str(row["name"]): row["pk"]
            for row in conn.execute(f"PRAGMA table_info({TABLE})")
        }
        assert columns == {
            "delivery_id": 1,
            "correlation_id": 0,
            "claimed_at": 0,
            "terminal_json": 0,
        }
        assert [
            tuple(row) for row in conn.execute(f"SELECT * FROM {LEGACY_TABLE}")
        ] == [OLD_ROW]
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0
        assert {PENDING_INDEX, CORRELATION_INDEX} <= _indexes(conn, TABLE)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM omnimarket_sqlite_store_steps WHERE step = ?",
                (STEP,),
            ).fetchone()[0]
            == 1
        )
        conn.execute(f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?)", NEW_ROW)
        conn.commit()

    with closing(adapter._connect()) as conn:
        assert _legacy_tables(conn) == {LEGACY_TABLE}
        assert [
            tuple(row) for row in conn.execute(f"SELECT * FROM {LEGACY_TABLE}")
        ] == [OLD_ROW]
        assert [tuple(row) for row in conn.execute(f"SELECT * FROM {TABLE}")] == [
            NEW_ROW
        ]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM omnimarket_sqlite_store_steps WHERE step = ?",
                (STEP,),
            ).fetchone()[0]
            == 1
        )


def test_claims_table_old_shape_releases_explicit_indexes(
    old_shape_store: Path,
) -> None:
    with closing(sqlite3.connect(old_shape_store)) as conn:
        conn.execute(f"CREATE INDEX {CORRELATION_INDEX} ON {TABLE} (correlation_id)")
        conn.execute(f'CREATE INDEX "old claims ""tenant" ON {TABLE} (tenant_id)')
        conn.commit()

    with closing(SqliteDatabaseAdapter(old_shape_store)._connect()) as conn:
        assert {PENDING_INDEX, CORRELATION_INDEX} <= _indexes(conn, TABLE)
        assert all(
            row[0] is None
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
                (LEGACY_TABLE,),
            )
        )
        # The renamed table's primary-key autoindex still enforces uniqueness.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO {LEGACY_TABLE} VALUES (?, ?, ?, ?)", OLD_ROW)


@pytest.mark.parametrize("occupied_count", [1, 2])
def test_claims_table_old_shape_uses_first_free_legacy_name(
    old_shape_store: Path, occupied_count: int
) -> None:
    occupied = [LEGACY_TABLE] + [
        f"{LEGACY_TABLE}_{index}" for index in range(2, occupied_count + 1)
    ]
    with closing(sqlite3.connect(old_shape_store)) as conn:
        for table in occupied:
            conn.execute(f"CREATE TABLE {table} (evidence TEXT)")
            conn.execute(f"INSERT INTO {table} VALUES (?)", (table,))
        conn.commit()

    with closing(SqliteDatabaseAdapter(old_shape_store)._connect()) as conn:
        renamed = f"{LEGACY_TABLE}_{occupied_count + 1}"
        assert _legacy_tables(conn) == {*occupied, renamed}
        assert [tuple(row) for row in conn.execute(f"SELECT * FROM {renamed}")] == [
            OLD_ROW
        ]
        for table in occupied:
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] == [
                (table,)
            ]


def test_claims_table_old_shape_current_shape_untouched(tmp_path: Path) -> None:
    path = tmp_path / "claims.sqlite"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            f"CREATE TABLE {TABLE} ("
            "delivery_id TEXT PRIMARY KEY, "
            "correlation_id TEXT NOT NULL DEFAULT '', "
            "claimed_at TEXT NOT NULL, "
            "terminal_json TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?)", NEW_ROW)
        conn.commit()

    with closing(SqliteDatabaseAdapter(path)._connect()) as conn:
        assert _legacy_tables(conn) == set()
        assert [tuple(row) for row in conn.execute(f"SELECT * FROM {TABLE}")] == [
            NEW_ROW
        ]
        assert {PENDING_INDEX, CORRELATION_INDEX} <= _indexes(conn, TABLE)
        # The step runs once as a no-op on a current-shape store.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM omnimarket_sqlite_store_steps WHERE step = ?",
                (STEP,),
            ).fetchone()[0]
            == 1
        )
