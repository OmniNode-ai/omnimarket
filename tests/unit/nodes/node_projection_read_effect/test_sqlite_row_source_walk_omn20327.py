# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The SQLite row source speaks the OMN-20327 walk protocol.

OMN-20327 gave ``ProtocolProjectionRowSource`` a ``selection`` on ``rows`` and
a ``walk_origin``; the SQLite source (OMN-20329) landed without either, so
every read through ``read_page`` raised ``TypeError``. Each test names the
failure it exists to catch:

* a ``walk`` window that starts at the newest rows can never reach the older
  keys, so the walk silently skips them;
* a ``ranked`` window cut by recency serves the newest rows instead of the
  top-ranked ones;
* ``walk_origin`` must answer one below the tenant's smallest integer cursor,
  and another tenant's smaller cursor must not move it;
* a cursor with no integer value has no origin a caller could be handed;
* a missing table or store is a named refusal, and a read never creates the
  store it was pointed at.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import (
    RETAINED_WINDOW_FACTOR,
    ProjectionReadError,
)

_TOPIC = "onex.snapshot.projection.walk.rows.v1"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_OTHER_TENANT = "11111111-2222-4333-8444-555555555555"
_COLUMNS = ("id", "tenant_id", "written_at", "score")
_LIMIT = 1
_ROWS = RETAINED_WINDOW_FACTOR * _LIMIT + 2


def _cfg(**overrides: Any) -> ProjectionTableConfig:
    fields: dict[str, Any] = {
        "topic": _TOPIC,
        "table": "walk_rows",
        "schema_name": "public",
        "relation_schema": "public",
        "columns": _COLUMNS,
        "order_by": "id ASC",
        "order_by_spec": parse_order_by_clauses("id ASC", _COLUMNS),
        "freshness_column": "written_at",
        "cursor_column": "id",
        "limit": _LIMIT,
        "bus_backed": True,
        "key_columns": ("id",),
        "tenant_column": "tenant_id",
    }
    fields.update(overrides)
    return ProjectionTableConfig(**fields)


def _store(tmp_path: Path) -> Path:
    """Six rows for the tenant (ids 10..15) and one smaller id for another."""
    db_path = tmp_path / "walk.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE walk_rows (id INTEGER NOT NULL UNIQUE, tenant_id TEXT, "
            "written_at TEXT, score REAL)"
        )
        for i in range(_ROWS):
            conn.execute(
                "INSERT INTO walk_rows VALUES (?, ?, ?, ?)",
                # the oldest rows score highest, so ranked and newest windows differ
                (10 + i, _TENANT, f"2026-10-01T12:0{i}:00+00:00", float(_ROWS - i)),
            )
        conn.execute(
            "INSERT INTO walk_rows VALUES (?, ?, ?, ?)",
            (3, _OTHER_TENANT, "2026-10-01T11:00:00+00:00", 0.0),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _ids(rows: list[dict[str, Any]]) -> list[int]:
    return sorted(int(row["id"]) for row in rows)


async def test_walk_window_starts_at_the_oldest_rows(tmp_path: Path) -> None:
    source = SqliteTableRowSource(_store(tmp_path))
    rows = await source.rows(
        _cfg(), order_spec=(("id", "ASC", None),), tenant_id=_TENANT, selection="walk"
    )
    assert _ids(rows) == [10, 11, 12, 13]


async def test_newest_window_is_unchanged(tmp_path: Path) -> None:
    source = SqliteTableRowSource(_store(tmp_path))
    rows = await source.rows(
        _cfg(), order_spec=(("id", "ASC", None),), tenant_id=_TENANT
    )
    assert _ids(rows) == [12, 13, 14, 15]


async def test_ranked_window_serves_the_top_ranked_rows(tmp_path: Path) -> None:
    source = SqliteTableRowSource(_store(tmp_path))
    rows = await source.rows(
        _cfg(),
        order_spec=(("score", "DESC", None),),
        tenant_id=_TENANT,
        selection="ranked",
    )
    assert _ids(rows) == [10, 11, 12, 13]
    assert [row["score"] for row in rows] == sorted(
        (row["score"] for row in rows), reverse=True
    )


async def test_walk_origin_is_one_below_the_tenants_smallest_cursor(
    tmp_path: Path,
) -> None:
    source = SqliteTableRowSource(_store(tmp_path))
    assert await source.walk_origin(_cfg(), tenant_id=_TENANT) == "9"
    assert await source.walk_origin(_cfg(), tenant_id=_OTHER_TENANT) == "2"


async def test_walk_origin_has_no_value_without_an_integer_cursor(
    tmp_path: Path,
) -> None:
    source = SqliteTableRowSource(_store(tmp_path))
    assert (
        await source.walk_origin(_cfg(cursor_column="written_at"), tenant_id=_TENANT)
        is None
    )
    assert await source.walk_origin(_cfg(cursor_column=None), tenant_id=_TENANT) is None


async def test_walk_origin_refuses_a_missing_table_by_name(tmp_path: Path) -> None:
    source = SqliteTableRowSource(_store(tmp_path))
    with pytest.raises(ProjectionReadError) as refused:
        await source.walk_origin(_cfg(table="absent_rows"), tenant_id=_TENANT)
    assert refused.value.code == "projection_table_missing"


async def test_walk_origin_never_creates_the_store(tmp_path: Path) -> None:
    missing = tmp_path / "never.sqlite"
    source = SqliteTableRowSource(missing)
    with pytest.raises(ProjectionReadError) as refused:
        await source.walk_origin(_cfg(), tenant_id=_TENANT)
    assert refused.value.code == "projection_database_unavailable"
    assert not missing.exists()
