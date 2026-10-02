# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection read node over the local SQLite store (OMN-20329).

Local MVP mode 1 runs the ``onex`` process with an in-process bus and a SQLite
store, and decision D1 (a) routes the local dashboard's reads through this node
(OMN-20159). Each test names the failure it exists to catch:

* a binding that names a SQLite file must serve a declared exposure's rows from
  it, scoped to the request's tenant, rather than refuse the binding;
* a Postgres binding must still resolve to the Postgres row source, and any
  other scheme must still be refused by name;
* a declared exposure whose table the local store never created answers a
  named refusal, never ``ok`` with zero rows;
* a read must never create the store file it was pointed at.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.nodes.node_projection_read_effect.ports.read_source_resolution import (
    resolve_projection_read_source,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
    build_sqlite_window_query,
)
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.sqlite_database import sqlite_path_from_dsn
from omnimarket.projection.table_reader import ProjectionReadError, TableRowSource

_OVERLAY_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_UNSCOPED = "onex.snapshot.projection.unscoped.v1"
_UNCREATED = "onex.snapshot.projection.uncreated.v1"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_OTHER_TENANT = "11111111-2222-4333-8444-555555555555"
_COLUMNS = ("correlation_id", "tenant_id", "written_at", "cost_usd")


def _cfg(topic: str, **overrides: Any) -> ProjectionTableConfig:
    fields: dict[str, Any] = {
        "topic": topic,
        "table": "delegation_events",
        "schema_name": "public",
        "relation_schema": "public",
        "columns": _COLUMNS,
        "order_by": "written_at DESC",
        "order_by_spec": parse_order_by_clauses("written_at DESC", _COLUMNS),
        "freshness_column": "written_at",
        "cursor_column": "written_at",
        "limit": 500,
        "bus_backed": True,
        "key_columns": ("correlation_id",),
        "tenant_column": "tenant_id",
    }
    fields.update(overrides)
    return ProjectionTableConfig(**fields)


def _topic_map() -> dict[str, ProjectionTableConfig]:
    return {
        _DECISIONS: _cfg(_DECISIONS),
        _UNSCOPED: _cfg(_UNSCOPED, tenant_column=None),
        _UNCREATED: _cfg(_UNCREATED, table="llm_call_metrics", tenant_column=None),
    }


def _correlation(tenant_index: int, i: int) -> str:
    return f"20329000-0000-4000-8000-0000000{tenant_index}000{i}"


def _store(tmp_path: Path) -> Path:
    """A local store holding three rows for each of two tenants."""
    db_path = tmp_path / "delegation.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE delegation_events (correlation_id TEXT NOT NULL UNIQUE, "
            "tenant_id TEXT, written_at TEXT, cost_usd REAL, extra TEXT)"
        )
        for tenant_index, tenant in enumerate((_TENANT, _OTHER_TENANT)):
            for i in range(3):
                conn.execute(
                    "INSERT INTO delegation_events VALUES (?, ?, ?, ?, ?)",
                    (
                        _correlation(tenant_index, i),
                        tenant,
                        f"2026-10-01T12:0{i}:00+00:00",
                        0.01 * (i + 1),
                        "not exposed",
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _bind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_url: str) -> None:
    overlay = tmp_path / "projection_binding.yaml"
    overlay.write_text(
        f"kafka_bootstrap_servers: inmemory\ndatabase_url: {database_url!r}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_OVERLAY_ENV, str(overlay))


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


async def test_sqlite_binding_serves_the_tenants_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bind(monkeypatch, tmp_path, _sqlite_url(_store(tmp_path)))
    handler = HandlerProjectionRead(topic_map=_topic_map())
    try:
        result = await handler.handle(
            ModelProjectionReadRequest(topic=_DECISIONS, tenant_id=_TENANT)
        )
    finally:
        await handler.close()
    assert result.ok is True, result
    assert result.tenant == _TENANT
    assert [row["correlation_id"] for row in result.rows] == [
        _correlation(0, i) for i in (2, 1, 0)
    ]
    assert {row["tenant_id"] for row in result.rows} == {_TENANT}
    assert set(result.rows[0]) == set(_COLUMNS), "only declared columns are served"


async def test_sqlite_since_walk_and_correlation_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bind(monkeypatch, tmp_path, _sqlite_url(_store(tmp_path)))
    handler = HandlerProjectionRead(topic_map=_topic_map())
    walked = await handler.handle(
        ModelProjectionReadRequest(
            topic=_UNSCOPED, since="2026-10-01T12:00:00+00:00", order="asc"
        )
    )
    filtered = await handler.handle(
        ModelProjectionReadRequest(
            topic=_DECISIONS,
            tenant_id=_TENANT,
            row_correlation_id=_correlation(0, 1),
        )
    )
    assert walked.ok is True, walked
    assert sorted(row["correlation_id"] for row in walked.rows) == sorted(
        _correlation(t, i) for t in (0, 1) for i in (1, 2)
    )
    assert filtered.ok is True, filtered
    assert [row["correlation_id"] for row in filtered.rows] == [_correlation(0, 1)]


def _cursor_store(
    tmp_path: Path, cursors: tuple[object, ...] = tuple(range(1, 9))
) -> tuple[Path, ProjectionTableConfig]:
    db_path = tmp_path / "cursors.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        cursor_type = (
            "TEXT" if any(isinstance(value, str) for value in cursors) else "INTEGER"
        )
        conn.execute(
            f"CREATE TABLE cursor_events (cursor {cursor_type}, tenant_id, rank INTEGER)"
        )
        ranks = (8, 1, 7, 2, 6, 3, 5, 4)
        conn.executemany(
            "INSERT INTO cursor_events VALUES (?, ?, ?)",
            [(cursor, 7, ranks[i]) for i, cursor in enumerate(cursors)],
        )
        conn.execute("INSERT INTO cursor_events VALUES (-100, 8, 100)")
        conn.commit()
    finally:
        conn.close()
    columns = ("cursor", "tenant_id", "rank")
    cfg = _cfg(
        _DECISIONS,
        table="cursor_events",
        columns=columns,
        cursor_column="cursor",
        freshness_column=None,
        order_by="rank DESC",
        order_by_spec=parse_order_by_clauses("rank DESC", columns),
        key_columns=("cursor",),
        limit=1,
    )
    return db_path, cfg


@pytest.mark.parametrize(
    ("selection", "since", "expected"),
    [
        (None, None, [5, 7, 8, 6]),
        ("newest", None, [5, 7, 8, 6]),
        ("walk", None, [1, 3, 4, 2]),
        ("ranked", None, [1, 3, 5, 7]),
        ("newest", "3", [5, 7, 6, 4]),
        ("walk", "3", [5, 7, 6, 4]),
        ("ranked", "3", [5, 7, 6, 4]),
    ],
)
async def test_sqlite_selection_controls_the_retained_window(
    tmp_path: Path, selection: str | None, since: str | None, expected: list[int]
) -> None:
    db_path, cfg = _cursor_store(tmp_path)
    kwargs = {} if selection is None else {"selection": selection}
    rows = await SqliteTableRowSource(db_path).rows(
        cfg, order_spec=cfg.order_by_spec, tenant_id="7", since=since, **kwargs
    )
    assert [row["cursor"] for row in rows] == expected
    assert {row["tenant_id"] for row in rows} == {7}


@pytest.mark.parametrize(
    ("freshness_column", "expected"),
    [("rank", [8, 6, 4, 2]), (None, [1, 3, 5, 7])],
)
async def test_sqlite_walk_without_cursor_uses_freshness_then_declared_order(
    tmp_path: Path, freshness_column: str | None, expected: list[int]
) -> None:
    db_path, cfg = _cursor_store(tmp_path)
    cfg = cfg.model_copy(
        update={"cursor_column": None, "freshness_column": freshness_column}
    )
    rows = await SqliteTableRowSource(db_path).rows(
        cfg, order_spec=cfg.order_by_spec, tenant_id="7", selection="walk"
    )
    assert [row["cursor"] for row in rows] == expected


@pytest.mark.parametrize("selection", ["newest", "walk", "ranked"])
def test_sqlite_since_still_requires_a_cursor(selection: str) -> None:
    cfg = _cfg(_UNSCOPED, tenant_column=None, cursor_column=None)
    with pytest.raises(ProjectionReadError) as raised:
        build_sqlite_window_query(
            cfg,
            order_spec=cfg.order_by_spec,
            tenant_id=None,
            since="1",
            selection=selection,
        )
    assert raised.value.code == "unsupported_filter"
    assert raised.value.status_code == 422


@pytest.mark.parametrize(
    ("cursors", "expected"),
    [
        ((12, 4, 8), "3"),
        ((0, 4), "-1"),
        ((-5, 4), "-6"),
        ((1.5, 2.5), None),
        (("2026-10-01", "2026-10-02"), None),
        (("4", "8"), None),
        ((None,), None),
        ((), None),
    ],
)
async def test_sqlite_walk_origin_uses_the_tenants_integer_minimum(
    tmp_path: Path, cursors: tuple[object, ...], expected: str | None
) -> None:
    db_path, cfg = _cursor_store(tmp_path, cursors)
    assert (
        await SqliteTableRowSource(db_path).walk_origin(cfg, tenant_id="7") == expected
    )


async def test_sqlite_walk_origin_can_be_unscoped(tmp_path: Path) -> None:
    db_path, cfg = _cursor_store(tmp_path)
    source = SqliteTableRowSource(db_path)
    assert await source.walk_origin(cfg, tenant_id=None) == "-101"
    cfg = cfg.model_copy(update={"tenant_column": None})
    assert await source.walk_origin(cfg, tenant_id="7") == "-101"


async def test_sqlite_walk_origin_rejects_a_boolean_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, cfg = _cursor_store(tmp_path)
    source = SqliteTableRowSource(db_path)
    conn = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = (True,)
    conn.execute.side_effect = [
        [(i, name) for i, name in enumerate(cfg.columns)],
        result,
    ]
    monkeypatch.setattr(source, "_connect", lambda: conn)
    assert await source.walk_origin(cfg, tenant_id="7") is None
    conn.close.assert_called_once()


async def test_sqlite_walk_origin_without_cursor_does_not_open_the_store(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "absent.sqlite"
    cfg = _cfg(_DECISIONS, cursor_column=None)
    assert (
        await SqliteTableRowSource(absent).walk_origin(cfg, tenant_id=_TENANT) is None
    )
    assert not absent.exists()


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"table": "absent"}, "projection_table_missing"),
        ({"cursor_column": "absent"}, "projection_column_missing"),
    ],
)
async def test_sqlite_walk_origin_refuses_a_missing_table_or_cursor(
    tmp_path: Path, override: dict[str, str], error: str
) -> None:
    db_path, cfg = _cursor_store(tmp_path)
    cfg = cfg.model_copy(update=override)
    with pytest.raises(ProjectionReadError) as raised:
        await SqliteTableRowSource(db_path).walk_origin(cfg, tenant_id="7")
    assert raised.value.code == error


@pytest.mark.parametrize("exists", [False, True])
async def test_sqlite_walk_origin_refuses_a_missing_or_corrupt_store(
    tmp_path: Path, exists: bool
) -> None:
    db_path = tmp_path / "unavailable.sqlite"
    if exists:
        db_path.write_text("not a SQLite database", encoding="utf-8")
    with pytest.raises(ProjectionReadError) as raised:
        await SqliteTableRowSource(db_path).walk_origin(
            _cfg(_DECISIONS), tenant_id=_TENANT
        )
    assert raised.value.code == "projection_database_unavailable"
    assert db_path.exists() is exists
    assert "not a database" not in raised.value.detail


async def test_declared_table_the_store_never_created_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bind(monkeypatch, tmp_path, _sqlite_url(_store(tmp_path)))
    result = await HandlerProjectionRead(topic_map=_topic_map()).handle(
        ModelProjectionReadRequest(topic=_UNCREATED)
    )
    assert result.ok is False
    assert result.error == "projection_table_missing"
    assert result.row_count == 0


async def test_declared_column_the_store_lacks_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # SQLite reads a quoted unknown column as a string literal, so without a
    # catalogue check this read would answer ok with the column's name as data.
    _bind(monkeypatch, tmp_path, _sqlite_url(_store(tmp_path)))
    topic_map = {
        _UNSCOPED: _cfg(
            _UNSCOPED,
            tenant_column=None,
            columns=(*_COLUMNS, "cost_tier_name"),
        )
    }
    result = await HandlerProjectionRead(topic_map=topic_map).handle(
        ModelProjectionReadRequest(topic=_UNSCOPED)
    )
    assert result.ok is False
    assert result.error == "projection_column_missing"
    assert result.row_count == 0


async def test_a_read_never_creates_the_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    absent = tmp_path / "absent" / "delegation.sqlite"
    _bind(monkeypatch, tmp_path, _sqlite_url(absent))
    result = await HandlerProjectionRead(topic_map=_topic_map()).handle(
        ModelProjectionReadRequest(topic=_DECISIONS, tenant_id=_TENANT)
    )
    assert result.ok is False
    assert result.error == "projection_database_unavailable"
    assert not absent.exists()


def test_postgres_binding_still_resolves_to_the_postgres_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bind(monkeypatch, tmp_path, "postgresql://nobody@127.0.0.1:1/none")
    assert isinstance(resolve_projection_read_source(), TableRowSource)


def test_sqlite_binding_resolves_to_the_sqlite_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = _store(tmp_path)
    _bind(monkeypatch, tmp_path, _sqlite_url(db_path))
    source = resolve_projection_read_source()
    assert isinstance(source, SqliteTableRowSource)
    assert source.db_path == db_path


def test_other_schemes_are_still_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bind(monkeypatch, tmp_path, "mysql://nobody@127.0.0.1:1/none")
    with pytest.raises(ProjectionReadError) as raised:
        resolve_projection_read_source()
    assert raised.value.code == "projection_binding_unsupported"


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("sqlite:////abs/delegation.sqlite", Path("/abs/delegation.sqlite")),
        ("sqlite:///rel/delegation.sqlite", Path("rel/delegation.sqlite")),
        ("file:///abs/delegation.sqlite", Path("abs/delegation.sqlite")),
    ],
)
def test_sqlite_path_follows_the_writer_convention(dsn: str, expected: Path) -> None:
    assert sqlite_path_from_dsn(dsn) == expected
