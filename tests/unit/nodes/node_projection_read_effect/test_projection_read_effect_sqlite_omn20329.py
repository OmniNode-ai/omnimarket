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


def test_sqlite_window_selection_orders_the_inner_window() -> None:
    cfg = _cfg(_DECISIONS)
    order_spec = parse_order_by_clauses("cost_usd DESC", _COLUMNS)
    walked = build_sqlite_window_query(
        cfg, order_spec=order_spec, tenant_id=_TENANT, selection="walk"
    )
    ranked = build_sqlite_window_query(
        cfg, order_spec=order_spec, tenant_id=_TENANT, selection="ranked"
    )
    assert 'ORDER BY "written_at" ASC LIMIT 2000) AS served_window' in walked.sql
    assert (
        'ORDER BY "cost_usd" DESC NULLS LAST LIMIT 2000) AS served_window' in ranked.sql
    )
    assert walked.sql.endswith('ORDER BY "cost_usd" DESC NULLS LAST')
    assert ranked.sql.endswith('ORDER BY "cost_usd" DESC NULLS LAST')


async def test_sqlite_walk_origin_starts_before_the_tenants_integer_cursor(
    tmp_path: Path,
) -> None:
    db_path = _store(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE delegation_events ADD COLUMN event_sequence INTEGER")
        conn.executemany(
            "UPDATE delegation_events SET event_sequence = ? WHERE correlation_id = ?",
            [
                (offset + i, _correlation(tenant_index, i))
                for tenant_index, offset in ((0, 10), (1, 1))
                for i in range(3)
            ],
        )
        conn.commit()
    finally:
        conn.close()
    source = SqliteTableRowSource(db_path)
    cfg = _cfg(_DECISIONS, cursor_column="event_sequence")
    assert await source.walk_origin(cfg, tenant_id=_TENANT) == "9"
    assert await source.walk_origin(cfg, tenant_id=_OTHER_TENANT) == "0"
    assert await source.walk_origin(cfg, tenant_id=None) == "0"
    assert await source.walk_origin(cfg, tenant_id="absent") is None
    assert await source.walk_origin(_cfg(_DECISIONS), tenant_id=_TENANT) is None
    assert (
        await source.walk_origin(
            _cfg(_DECISIONS, cursor_column=None), tenant_id=_TENANT
        )
        is None
    )


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


async def test_sqlite_source_serves_the_walk_and_ranked_selections(
    tmp_path: Path,
) -> None:
    source = SqliteTableRowSource(_store(tmp_path))
    cfg = _cfg(_UNSCOPED, tenant_column=None, limit=1)
    order_spec = cfg.order_by_spec
    newest = await source.rows(cfg, order_spec=order_spec, tenant_id=None)
    walk = await source.rows(
        cfg, order_spec=order_spec, tenant_id=None, selection="walk"
    )
    ranked = await source.rows(
        cfg, order_spec=order_spec, tenant_id=None, selection="ranked"
    )
    assert {row["written_at"] for row in newest} == {
        "2026-10-01T12:01:00+00:00",
        "2026-10-01T12:02:00+00:00",
    }
    assert {row["written_at"] for row in walk} == {
        "2026-10-01T12:00:00+00:00",
        "2026-10-01T12:01:00+00:00",
    }
    assert {row["written_at"] for row in ranked} == {
        row["written_at"] for row in newest
    }


async def test_sqlite_walk_origin_is_one_below_an_integer_cursor(
    tmp_path: Path,
) -> None:
    db_path = _store(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE ledger (seq INTEGER, tenant_id TEXT)")
        conn.executemany(
            "INSERT INTO ledger VALUES (?, ?)",
            [(7, _TENANT), (9, _TENANT), (3, _OTHER_TENANT)],
        )
        conn.commit()
    finally:
        conn.close()
    source = SqliteTableRowSource(db_path)
    ledger = _cfg(
        _DECISIONS,
        table="ledger",
        columns=("seq", "tenant_id"),
        order_by="seq DESC",
        order_by_spec=parse_order_by_clauses("seq DESC", ("seq", "tenant_id")),
        freshness_column=None,
        cursor_column="seq",
        key_columns=("seq",),
    )
    assert await source.walk_origin(ledger, tenant_id=_TENANT) == "6"
    assert await source.walk_origin(ledger, tenant_id=_OTHER_TENANT) == "2"
    text_cursor = _cfg(_UNSCOPED, tenant_column=None)
    assert await source.walk_origin(text_cursor, tenant_id=None) is None
