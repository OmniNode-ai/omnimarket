# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19971: the newest window and the unique-key read against a real PostgreSQL.

On the dev lane (2026-10-03) ``delegation.decisions`` served the newest rows of
its OLDEST window, and ``work.events``, ``session.replay`` and ``live-events``
timed out walking their whole tables one key at a time. What only a real
database proves:

* F6 an exposure with no cursor, holding more than ``limit * 4`` rows, serves
  its newest rows and its newest ``latest_event_at``, with or without a unique
  index on its key;
* F7 an exposure with a cursor still starts its walk at the first row and
  pages by cursor;
* F8 a key covered by a unique index (or declared immutable) is read from the
  relation directly and serves its newest rows, while a mutable key without
  one -- or with only a partial or an invalid unique index -- still gets one
  row per key, and a unique key over a nullable column never serves the rows
  whose key is NULL;
* F9 one row with a NULL freshness value does not hide the newest one, and a
  newest window holding only such rows still reports the newest dated value;
* F10 the unique-index lookup reads the real catalogue, and a relation that
  does not exist is still refused by name;
* F11 a unique index that compares the key under another collation or
  operator class than the column's own does not count (the real catalogue
  prints its key column as the bare column name);
* F12 a unique index dropped while the reader runs stops the fast path once
  the cached catalogue answer has aged out;
* F13 a cursor exposure's ``latest_event_at`` is read from its newest rows by
  cursor, not by ordering the relation on its freshness column.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import asyncpg
import pytest

from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.read_page import read_projection_page
from omnimarket.projection.table_reader import (
    DEFAULT_DSN_ENV,
    RETAINED_WINDOW_FACTOR,
    ProjectionReadError,
    TableRowSource,
    UniqueKeyCatalogue,
)
from tests.test_omn15359_ac3_replay_real_postgres import local_postgres

_SCHEMA = "omn19971_amendment2"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_LIMIT = 5
_RETAIN = _LIMIT * RETAINED_WINDOW_FACTOR
_ROWS = _RETAIN * 3
_NULL_KEYED = 3
_EPOCH = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _decisions_cfg(table: str) -> ProjectionTableConfig:
    columns = ("correlation_id", "tenant_id", "model_name", "written_at")
    return ProjectionTableConfig(
        topic=f"onex.snapshot.projection.omn19971.{table}.v1",
        table=table,
        schema_name="public",
        relation_schema=_SCHEMA,
        columns=columns,
        order_by="written_at DESC",
        order_by_spec=parse_order_by_clauses("written_at DESC", columns),
        freshness_column="written_at",
        limit=_LIMIT,
        bus_backed=True,
        key_columns=("correlation_id",),
        key_grain="mutable",
        tenant_column="tenant_id",
    )


def _events_cfg(
    table: str, *, key_grain: Literal["immutable", "mutable"]
) -> ProjectionTableConfig:
    columns = ("event_id", "type", "created_at")
    return ProjectionTableConfig(
        topic=f"onex.snapshot.projection.omn19971.{table}.v1",
        table=table,
        schema_name="public",
        relation_schema=_SCHEMA,
        columns=columns,
        order_by="created_at DESC",
        order_by_spec=parse_order_by_clauses("created_at DESC", columns),
        freshness_column="created_at",
        limit=_LIMIT,
        bus_backed=True,
        key_columns=("event_id",),
        key_grain=key_grain,
    )


def _flow_cfg(table: str, *, latest_by: str | None = None) -> ProjectionTableConfig:
    columns = (
        "projection_cursor",
        "consumer_group",
        "topic",
        "window_start",
        "window_end",
    )
    return ProjectionTableConfig(
        topic=f"onex.snapshot.projection.omn19971.{table}.v1",
        table=table,
        schema_name="public",
        relation_schema=_SCHEMA,
        columns=columns,
        order_by="window_end DESC",
        order_by_spec=parse_order_by_clauses("window_end DESC", columns),
        freshness_column="window_end",
        cursor_column="projection_cursor",
        limit=_LIMIT,
        bus_backed=True,
        key_columns=("consumer_group", "topic"),
        key_grain="mutable",
        latest_by=latest_by,
    )


@pytest.fixture
def dsn(request: pytest.FixtureRequest) -> str:
    """CI's INTEGRATION_POSTGRES_* database when set, else a disposable local one."""
    password = os.environ.get("INTEGRATION_POSTGRES_PASSWORD")
    if password:
        host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
        port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
        database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
        user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    # The imported disposable-PostgreSQL fixture, requested by its own name.
    pg = request.getfixturevalue(local_postgres.__name__)[0]
    return f"postgresql://postgres@/{pg.database}?host={pg.host}"


@pytest.fixture
async def seeded(dsn: str) -> AsyncIterator[None]:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        # delegation.decisions' shape twice: with the unique index the real
        # table has, and without one (the per-key read must still be newest).
        for table, unique in (
            ("delegation_events", " UNIQUE"),
            ("delegation_events_no_index", ""),
        ):
            await conn.execute(
                f"CREATE TABLE {_SCHEMA}.{table} ("
                f" correlation_id text NOT NULL{unique}, tenant_id text NOT NULL,"
                " model_name text NOT NULL, written_at timestamptz NOT NULL)"
            )
            await conn.executemany(
                f"INSERT INTO {_SCHEMA}.{table} VALUES ($1, $2, $3, $4)",
                [
                    (f"corr-{i:04d}", _TENANT, "qwen", _EPOCH + timedelta(minutes=i))
                    for i in range(_ROWS)
                ],
            )
        # live-events' shape: a surrogate primary key, and the key unique.
        # work.events' shape: the key is the primary key, grain immutable.
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.live_events ("
            " id bigserial PRIMARY KEY, event_id text NOT NULL UNIQUE,"
            " type text NOT NULL, created_at timestamptz NOT NULL)"
        )
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.work_events ("
            " event_id text PRIMARY KEY, type text NOT NULL,"
            " created_at timestamptz NOT NULL)"
        )
        for table in ("live_events", "work_events"):
            await conn.executemany(
                f"INSERT INTO {_SCHEMA}.{table} (event_id, type, created_at)"
                " VALUES ($1, $2, $3)",
                [
                    (f"evt-{i:04d}", "tool", _EPOCH + timedelta(seconds=i))
                    for i in range(_ROWS)
                ],
            )
        # A cursor exposure with one window per key: the walk pages by cursor.
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.flow_walk ("
            " projection_cursor bigserial PRIMARY KEY, consumer_group text NOT NULL,"
            " topic text NOT NULL, window_start timestamptz NOT NULL,"
            " window_end timestamptz NOT NULL)"
        )
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.flow_walk"
            " (consumer_group, topic, window_start, window_end)"
            " VALUES ($1, $2, $3, $4)",
            [
                (
                    f"group-{i:03d}",
                    "t",
                    _EPOCH + timedelta(minutes=i),
                    _EPOCH + timedelta(minutes=i + 1),
                )
                for i in range(30)
            ],
        )
        # The same shape with one row out of time order: the OLDEST row by
        # cursor carries a window_end far past every other row's, so "the
        # newest rows by cursor" and "the relation ordered by window_end"
        # give different answers.
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.flow_out_of_order ("
            " projection_cursor bigserial PRIMARY KEY, consumer_group text NOT NULL,"
            " topic text NOT NULL, window_start timestamptz NOT NULL,"
            " window_end timestamptz NOT NULL)"
        )
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.flow_out_of_order"
            " (consumer_group, topic, window_start, window_end)"
            " VALUES ($1, $2, $3, $4)",
            [
                ("group-early", "t", _EPOCH, _EPOCH + timedelta(days=365)),
                *(
                    (
                        f"group-{i:03d}",
                        "t",
                        _EPOCH + timedelta(minutes=i),
                        _EPOCH + timedelta(minutes=i + 1),
                    )
                    for i in range(_RETAIN + 5)
                ),
            ],
        )
        # A mutable key holding many revisions, with only a PARTIAL unique
        # index on the key: it does not make the key unique for every row.
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.flow_revisions ("
            " projection_cursor bigserial PRIMARY KEY, consumer_group text NOT NULL,"
            " topic text NOT NULL, window_start timestamptz NOT NULL,"
            " window_end timestamptz NOT NULL)"
        )
        await conn.execute(
            f"CREATE UNIQUE INDEX flow_revisions_open_key ON {_SCHEMA}.flow_revisions"
            " (consumer_group, topic) WHERE window_end > '2030-01-01'"
        )
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.flow_revisions"
            " (consumer_group, topic, window_start, window_end)"
            " VALUES ($1, $2, $3, $4)",
            [
                (
                    group,
                    "t",
                    _EPOCH + timedelta(minutes=revision),
                    _EPOCH + timedelta(minutes=revision + 1),
                )
                for revision in range(10)
                for group in ("busy", "quiet", "idle")
            ],
        )
        # The same revisions under an INVALID unique index on the key: a
        # concurrent build over duplicate keys fails and leaves the index
        # behind, marked invalid. CONCURRENTLY runs outside a transaction,
        # which a single-statement execute() without arguments is. The table
        # is declared in full so it carries the cursor's primary key (LIKE ...
        # INCLUDING DEFAULTS copies no index).
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.flow_invalid ("
            " projection_cursor bigserial PRIMARY KEY, consumer_group text NOT NULL,"
            " topic text NOT NULL, window_start timestamptz NOT NULL,"
            " window_end timestamptz NOT NULL)"
        )
        await conn.execute(
            f"INSERT INTO {_SCHEMA}.flow_invalid"
            " (consumer_group, topic, window_start, window_end)"
            " SELECT consumer_group, topic, window_start, window_end"
            f" FROM {_SCHEMA}.flow_revisions ORDER BY projection_cursor"
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY flow_invalid_key"
                f" ON {_SCHEMA}.flow_invalid (consumer_group, topic)"
            )
        # Positive control: the index exists and the catalogue marks it invalid.
        assert (
            await conn.fetchval(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass($1)",
                f"{_SCHEMA}.flow_invalid_key",
            )
            is False
        )
        # live-events' shape with a NULLABLE unique key: the newest rows carry
        # no key, so a window cut straight from the relation would serve them.
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.live_events_nullable ("
            " id bigserial PRIMARY KEY, event_id text UNIQUE,"
            " type text NOT NULL, created_at timestamptz NOT NULL)"
        )
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.live_events_nullable (event_id, type, created_at)"
            " VALUES ($1, $2, $3)",
            [
                (
                    None if i >= _ROWS - _NULL_KEYED else f"evt-{i:04d}",
                    "tool",
                    _EPOCH + timedelta(seconds=i),
                )
                for i in range(_ROWS)
            ],
        )
        # delegation.decisions' shape with a nullable freshness column holding
        # NULLs (written_at is nullable on the real table, migration 0038).
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.decisions_null_written_at ("
            " correlation_id text NOT NULL UNIQUE, tenant_id text NOT NULL,"
            " model_name text NOT NULL, written_at timestamptz)"
        )
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.decisions_null_written_at VALUES ($1, $2, $3, $4)",
            [
                (
                    f"corr-{i:04d}",
                    _TENANT,
                    "qwen",
                    None if i % 2 else _EPOCH + timedelta(minutes=i),
                )
                for i in range(_ROWS)
            ],
        )
        # live-events' shape under a unique index that compares the key with
        # another collation, and under one with a non-default operator class.
        # Neither forbids two rows that are equal under the column's own
        # equality, which is what the per-key scan compares with.
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.live_events_other_collation ("
            ' id bigserial PRIMARY KEY, event_id text COLLATE "C" NOT NULL,'
            " type text NOT NULL, created_at timestamptz NOT NULL)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX live_events_other_collation_key"
            f' ON {_SCHEMA}.live_events_other_collation (event_id COLLATE "POSIX")'
        )
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.live_events_other_opclass ("
            " id bigserial PRIMARY KEY, event_id text NOT NULL,"
            " type text NOT NULL, created_at timestamptz NOT NULL)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX live_events_other_opclass_key"
            f" ON {_SCHEMA}.live_events_other_opclass (event_id text_pattern_ops)"
        )
        for table in ("live_events_other_collation", "live_events_other_opclass"):
            await conn.executemany(
                f"INSERT INTO {_SCHEMA}.{table} (event_id, type, created_at)"
                " VALUES ($1, $2, $3)",
                [
                    (f"evt-{i:04d}", "tool", _EPOCH + timedelta(seconds=i))
                    for i in range(_ROWS)
                ],
            )
            # Positive control: the index is there, valid, unique and whole,
            # and Postgres prints its key column as the bare column name.
            control = await conn.fetchrow(
                "SELECT indisunique AND indisvalid AND indpred IS NULL AS whole,"
                " pg_get_indexdef(indexrelid, 1, true) AS printed"
                " FROM pg_index WHERE indexrelid = to_regclass($1)",
                f"{_SCHEMA}.{table}_key",
            )
            assert control is not None
            assert tuple(control) == (True, "event_id")
        yield
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


async def _page(
    dsn: str, cfg: ProjectionTableConfig, **kwargs: Any
) -> tuple[int, dict[str, Any]]:
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        page = await read_projection_page(
            cfg.topic, topic_map={cfg.topic: cfg}, source=source, **kwargs
        )
    finally:
        await source.close()
    return page.status_code, page.body


def _catalogue(source: TableRowSource, table: str) -> UniqueKeyCatalogue:
    """What the row source read from the real catalogue for ``table``."""
    _, catalogue = source._unique_keys[f'"{_SCHEMA}"."{table}"']
    return catalogue


@pytest.mark.integration
@pytest.mark.parametrize("table", ["delegation_events", "delegation_events_no_index"])
async def test_f6_a_page_without_a_cursor_serves_the_newest_rows(
    seeded: None, dsn: str, table: str
) -> None:
    status, body = await _page(dsn, _decisions_cfg(table), tenant=_TENANT)

    assert status == 200, body
    newest = _EPOCH + timedelta(minutes=_ROWS - 1)
    served = [datetime.fromisoformat(row["written_at"]) for row in body["rows"]]
    assert served == [newest - timedelta(minutes=i) for i in range(_LIMIT)]
    assert body["latest_event_at"] == newest.isoformat()
    assert body["next_cursor"] is None


@pytest.mark.integration
async def test_f7_a_cursor_exposure_still_walks_from_the_first_row(
    seeded: None, dsn: str
) -> None:
    cfg = _flow_cfg("flow_walk")

    status, first = await _page(dsn, cfg)
    assert status == 200, first
    assert sorted(row["projection_cursor"] for row in first["rows"]) == [1, 2, 3, 4, 5]
    assert first["next_cursor"] == "5"
    # The walk's rows are the oldest; the freshness is still the table's newest.
    assert first["latest_event_at"] == (_EPOCH + timedelta(minutes=30)).isoformat()

    status, second = await _page(dsn, cfg, since=first["next_cursor"])
    assert status == 200, second
    assert sorted(row["projection_cursor"] for row in second["rows"]) == [
        6,
        7,
        8,
        9,
        10,
    ]


@pytest.mark.integration
async def test_f13_a_cursor_exposures_freshness_follows_its_cursor(
    seeded: None, dsn: str
) -> None:
    cfg = _flow_cfg("flow_out_of_order")

    status, first = await _page(dsn, cfg)

    assert status == 200, first
    # The premise, read back: the walk starts at the out-of-order row, whose
    # window_end is the relation's largest.
    largest = (_EPOCH + timedelta(days=365)).isoformat()
    assert max(row["window_end"] for row in first["rows"]) == largest
    # latest_event_at is the newest value among the newest limit * 4 rows by
    # cursor, the read the cursor's own index serves. The relation's largest
    # window_end lies outside them, and only ordering the whole relation by
    # window_end (which no index has to lead) would find it.
    newest_by_cursor = _EPOCH + timedelta(minutes=_RETAIN + 5)
    assert first["latest_event_at"] == newest_by_cursor.isoformat()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("table", "key_grain"),
    [("live_events", "mutable"), ("work_events", "immutable")],
)
async def test_f8_a_unique_key_is_read_straight_from_the_relation(
    seeded: None,
    dsn: str,
    table: str,
    key_grain: Literal["immutable", "mutable"],
) -> None:
    cfg = _events_cfg(table, key_grain=key_grain)
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        rows = await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
        if key_grain == "mutable":
            # The real catalogue answers the lookup: the key's own unique
            # index is found and the key is NOT NULL, so the window skips the
            # per-key scan.
            catalogue = _catalogue(source, table)
            assert frozenset({"event_id"}) in catalogue.unique_keys
            assert "event_id" in catalogue.not_null_columns
    finally:
        await source.close()

    served = [row["event_id"] for row in rows]
    assert served == [f"evt-{i:04d}" for i in range(_ROWS - 1, _ROWS - 1 - _RETAIN, -1)]


@pytest.mark.integration
@pytest.mark.parametrize("table", ["flow_revisions", "flow_invalid"])
@pytest.mark.parametrize("latest_by", [None, "window_start"])
async def test_f8_a_mutable_key_without_a_full_unique_index_keeps_one_row_per_key(
    seeded: None, dsn: str, table: str, latest_by: str | None
) -> None:
    cfg = _flow_cfg(table, latest_by=latest_by)
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        rows = await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
        # The partial (flow_revisions) or invalid (flow_invalid) unique index
        # on the key does not count; only the cursor's primary key does.
        assert _catalogue(source, table).unique_keys == (
            frozenset({"projection_cursor"}),
        )
    finally:
        await source.close()

    assert sorted(row["consumer_group"] for row in rows) == ["busy", "idle", "quiet"]
    newest_start = (_EPOCH + timedelta(minutes=9)).isoformat()
    assert all(row["window_start"] == newest_start for row in rows)


@pytest.mark.integration
async def test_f8_a_nullable_unique_key_never_serves_its_null_keyed_rows(
    seeded: None, dsn: str
) -> None:
    cfg = _events_cfg("live_events_nullable", key_grain="mutable")
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        rows = await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
        catalogue = _catalogue(source, "live_events_nullable")
        # The instrument reads the real catalogue: the index is there, and the
        # key column is known to be nullable.
        assert frozenset({"event_id"}) in catalogue.unique_keys
        assert "event_id" not in catalogue.not_null_columns
    finally:
        await source.close()

    # The per-key scan's rows: the newest keyed rows, none without a key.
    newest_keyed = _ROWS - _NULL_KEYED - 1
    served = [row["event_id"] for row in rows]
    assert None not in served
    assert served == [
        f"evt-{i:04d}" for i in range(newest_keyed, newest_keyed - _RETAIN, -1)
    ]


@pytest.mark.integration
async def test_f9_a_null_freshness_value_does_not_hide_the_newest_one(
    seeded: None, dsn: str
) -> None:
    cfg = _decisions_cfg("decisions_null_written_at")
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        latest = await source.latest_event_at(cfg, tenant_id=_TENANT)
    finally:
        await source.close()

    # Even minutes carry a value, odd ones NULL; the newest valued row is the
    # last even index.
    newest_valued = max(i for i in range(_ROWS) if i % 2 == 0)
    assert latest == _EPOCH + timedelta(minutes=newest_valued)


@pytest.mark.integration
async def test_f9_a_window_of_only_undated_rows_still_reports_the_newest_dated_value(
    seeded: None, dsn: str
) -> None:
    # Half the rows carry no written_at, more than the window keeps, and DESC
    # puts them first: the newest window holds no dated row at all.
    cfg = _decisions_cfg("decisions_null_written_at")

    status, body = await _page(dsn, cfg, tenant=_TENANT)

    assert status == 200, body
    # The premise, read back: page one holds rows and none of them is dated.
    assert len(body["rows"]) == _LIMIT
    assert all(row["written_at"] is None for row in body["rows"])
    newest_valued = max(i for i in range(_ROWS) if i % 2 == 0)
    assert (
        body["latest_event_at"]
        == (_EPOCH + timedelta(minutes=newest_valued)).isoformat()
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "table", ["live_events_other_collation", "live_events_other_opclass"]
)
async def test_f11_a_unique_index_under_another_equality_does_not_count(
    seeded: None, dsn: str, table: str
) -> None:
    cfg = _events_cfg(table, key_grain="mutable")
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        rows = await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
        catalogue = _catalogue(source, table)
        # The instrument reads the real catalogue: the surrogate primary key
        # is found. The key's own unique index is not counted, and the same
        # index with the column's own equality is (see the live_events case
        # of test_f8_a_unique_key_is_read_straight_from_the_relation).
        assert frozenset({"id"}) in catalogue.unique_keys
        assert frozenset({"event_id"}) not in catalogue.unique_keys
        assert "event_id" in catalogue.not_null_columns
    finally:
        await source.close()

    # The per-key scan still serves the newest rows, one per key.
    served = [row["event_id"] for row in rows]
    assert served == [f"evt-{i:04d}" for i in range(_ROWS - 1, _ROWS - 1 - _RETAIN, -1)]


@pytest.mark.integration
async def test_f12_a_dropped_unique_index_stops_the_fast_path_without_a_restart(
    seeded: None, dsn: str
) -> None:
    cfg = _events_cfg("live_events", key_grain="mutable")
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    now = [1_000.0]
    source._clock = lambda: now[0]
    conn = await asyncpg.connect(dsn)
    try:
        await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
        assert frozenset({"event_id"}) in _catalogue(source, "live_events").unique_keys

        # The key stops being unique: its index goes, and its newest key gets
        # a second, newer revision.
        await conn.execute(
            f"ALTER TABLE {_SCHEMA}.live_events DROP CONSTRAINT live_events_event_id_key"
        )
        await conn.execute(
            f"INSERT INTO {_SCHEMA}.live_events (event_id, type, created_at)"
            " VALUES ($1, $2, $3)",
            f"evt-{_ROWS - 1:04d}",
            "tool",
            _EPOCH + timedelta(seconds=_ROWS),
        )
        now[0] += 86_400.0
        rows = await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
        assert (
            frozenset({"event_id"}) not in _catalogue(source, "live_events").unique_keys
        )
    finally:
        await conn.close()
        await source.close()

    # One row per key again: the revised key is served once, by its newest row.
    served = [row["event_id"] for row in rows]
    assert len(served) == len(set(served))
    assert served[0] == f"evt-{_ROWS - 1:04d}"
    assert rows[0]["created_at"] == (_EPOCH + timedelta(seconds=_ROWS)).isoformat()


@pytest.mark.integration
async def test_f10_a_missing_relation_is_still_refused_by_name(
    seeded: None, dsn: str
) -> None:
    cfg = _events_cfg("never_written", key_grain="mutable")
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        with pytest.raises(ProjectionReadError) as refused:
            await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
        assert f'"{_SCHEMA}"."never_written"' not in source._unique_keys
    finally:
        await source.close()
    assert refused.value.code == "projection_table_missing"
