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
* F9 a newest window serves its dated rows before any undated one (Postgres
  reads ``DESC`` as NULLS FIRST), holds undated rows only where the dated
  ones leave room, and never runs the undated read when they fill it; its
  ``latest_event_at`` is the newest dated value, or ``None`` when the scope
  has no dated row;
* F10 the unique-index lookup reads the real catalogue and counts only a
  unique index, and a relation that does not exist is still refused by name;
* F11 a unique index that compares the key under another collation or
  operator class than the column's own does not count (the real catalogue
  prints its key column as the bare column name);
* F12 a unique index dropped while the reader runs still takes the fast path
  59 s later and stops it 61 s later, once the cached catalogue answer has
  aged out;
* F13 a cursor exposure's ``latest_event_at`` is read from its newest rows by
  cursor, not by ordering the relation on its freshness column.
"""

from __future__ import annotations

import json
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
    build_window_query,
)
from tests.test_omn15359_ac3_replay_real_postgres import local_postgres

_SCHEMA = "omn19971_amendment2"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_LIMIT = 5
_RETAIN = _LIMIT * RETAINED_WINDOW_FACTOR
_ROWS = _RETAIN * 3
_NULL_KEYED = 3
# decisions_few_dated: more dated rows than a page, and with its undated rows
# still fewer than the window keeps. decisions_undated: undated rows only.
_FEW_DATED = _LIMIT + 2
_FEW_UNDATED = 5
_ONLY_UNDATED = _LIMIT + 3
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
        # A plain, NON-unique index over the same key: valid, whole and over
        # plain columns, so a catalogue lookup that stopped asking for
        # indisunique would count it, and the catalogue and one-row-per-key
        # assertions on flow_revisions below would fail.
        await conn.execute(
            f"CREATE INDEX flow_revisions_key ON {_SCHEMA}.flow_revisions"
            " (consumer_group, topic)"
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
        # The same shape with few dated rows, and with undated rows only. All
        # three carry the real table's index on written_at, which the dated
        # read of a newest window goes through.
        for table in ("decisions_few_dated", "decisions_undated"):
            await conn.execute(
                f"CREATE TABLE {_SCHEMA}.{table} ("
                " correlation_id text NOT NULL UNIQUE, tenant_id text NOT NULL,"
                " model_name text NOT NULL, written_at timestamptz)"
            )
        for table in (
            "decisions_null_written_at",
            "decisions_few_dated",
            "decisions_undated",
        ):
            await conn.execute(
                f"CREATE INDEX {table}_written_at ON {_SCHEMA}.{table} (written_at)"
            )
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.decisions_few_dated VALUES ($1, $2, $3, $4)",
            [
                *(
                    (f"dated-{i:04d}", _TENANT, "qwen", _EPOCH + timedelta(minutes=i))
                    for i in range(_FEW_DATED)
                ),
                *(
                    (f"undated-{i:04d}", _TENANT, "qwen", None)
                    for i in range(_FEW_UNDATED)
                ),
            ],
        )
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.decisions_undated VALUES ($1, $2, $3, $4)",
            [(f"undated-{i:04d}", _TENANT, "qwen", None) for i in range(_ONLY_UNDATED)],
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


def _minutes(*offsets: int) -> list[str]:
    """``written_at`` values as a page serialises them."""
    return [(_EPOCH + timedelta(minutes=offset)).isoformat() for offset in offsets]


@pytest.mark.integration
async def test_f9_the_bounded_read_and_the_newest_window_agree_on_the_newest_value(
    seeded: None, dsn: str
) -> None:
    # Even minutes carry a value, odd ones NULL. The bounded read (no window
    # in hand) skips the NULLs; the newest window serves its dated rows
    # first, so the window alone gives the same answer.
    cfg = _decisions_cfg("decisions_null_written_at")
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        bounded = await source.latest_event_at(cfg, tenant_id=_TENANT)
        window = await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=_TENANT)
        from_window = await source.latest_event_at(
            cfg, tenant_id=_TENANT, window_rows=window
        )
    finally:
        await source.close()

    newest_valued = max(i for i in range(_ROWS) if i % 2 == 0)
    assert bounded == _EPOCH + timedelta(minutes=newest_valued)
    assert from_window == bounded


@pytest.mark.integration
async def test_f9_the_newest_window_serves_its_newest_dated_rows_before_undated_ones(
    seeded: None, dsn: str
) -> None:
    # More undated rows than the window keeps, and more dated ones. Postgres
    # reads DESC as NULLS FIRST, so a window cut by written_at DESC alone
    # would hold undated rows only.
    cfg = _decisions_cfg("decisions_null_written_at")
    dated = sorted((i for i in range(_ROWS) if i % 2 == 0), reverse=True)
    undated = _ROWS - len(dated)
    assert len(dated) > _RETAIN, "the premise: the dated rows fill the window"
    assert undated > _RETAIN, "the premise: so would the undated rows"
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        window = await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=_TENANT)
    finally:
        await source.close()

    status, body = await _page(dsn, cfg, tenant=_TENANT)

    assert [row["written_at"] for row in window] == _minutes(*dated[:_RETAIN])
    assert status == 200, body
    assert [row["written_at"] for row in body["rows"]] == _minutes(*dated[:_LIMIT])
    assert body["latest_event_at"] == _minutes(dated[0])[0]


@pytest.mark.integration
async def test_f9_a_window_with_room_holds_every_row_and_serves_undated_rows_last(
    seeded: None, dsn: str
) -> None:
    cfg = _decisions_cfg("decisions_few_dated")
    assert _FEW_DATED + _FEW_UNDATED <= _RETAIN, "the premise: every row fits"
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        window = await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=_TENANT)
        latest = await source.latest_event_at(
            cfg, tenant_id=_TENANT, window_rows=window
        )
    finally:
        await source.close()

    # Under the page's written_at DESC order the undated rows come last.
    assert [row["written_at"] for row in window] == [
        *_minutes(*range(_FEW_DATED - 1, -1, -1)),
        *([None] * _FEW_UNDATED),
    ]
    assert sorted(row["correlation_id"] for row in window[_FEW_DATED:]) == [
        f"undated-{i:04d}" for i in range(_FEW_UNDATED)
    ]
    assert latest == _EPOCH + timedelta(minutes=_FEW_DATED - 1)


@pytest.mark.integration
async def test_f9_a_scope_of_only_undated_rows_serves_them_with_no_latest_value(
    seeded: None, dsn: str
) -> None:
    cfg = _decisions_cfg("decisions_undated")
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        window = await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=_TENANT)
    finally:
        await source.close()

    status, body = await _page(dsn, cfg, tenant=_TENANT)

    assert sorted(row["correlation_id"] for row in window) == [
        f"undated-{i:04d}" for i in range(_ONLY_UNDATED)
    ]
    assert status == 200, body
    assert len(body["rows"]) == _LIMIT
    assert all(row["written_at"] is None for row in body["rows"])
    assert body["latest_event_at"] is None


def _undated_branch_scan(plan: dict[str, Any]) -> dict[str, Any]:
    """The relation scan under the undated branch of a newest window's plan.

    The ``Append``'s child ``Limit`` that is not the CTE scan, then that
    ``Limit``'s scan child; at both levels a child whose ``Parent
    Relationship`` is ``InitPlan`` (the CTE itself, the count of its rows) is
    skipped.
    """

    def find_append(node: dict[str, Any]) -> dict[str, Any] | None:
        if node["Node Type"] == "Append":
            return node
        for child in node.get("Plans", []):
            found = find_append(child)
            if found is not None:
                return found
        return None

    append = find_append(plan)
    assert append is not None, plan
    limits = [
        child
        for child in append.get("Plans", [])
        if child.get("Parent Relationship") != "InitPlan"
        and child["Node Type"] == "Limit"
    ]
    assert len(limits) == 1, append
    scans = [
        child
        for child in limits[0].get("Plans", [])
        if child.get("Parent Relationship") != "InitPlan"
    ]
    assert len(scans) == 1, limits[0]
    return scans[0]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("table", "loops"),
    [
        # 30 dated rows fill the 20-row window: the undated read never runs.
        ("decisions_null_written_at", 0),
        # 7 dated rows leave room: the undated read runs once.
        ("decisions_few_dated", 1),
    ],
)
async def test_f9_the_undated_read_runs_only_when_the_dated_rows_leave_room(
    seeded: None, dsn: str, table: str, loops: int
) -> None:
    cfg = _decisions_cfg(table)
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        # The statement the reader builds from the real catalogue.
        await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=_TENANT)
        catalogue = _catalogue(source, table)
        relation_columns = source._relation_columns[f'"{_SCHEMA}"."{table}"']
    finally:
        await source.close()
    query = build_window_query(
        cfg,
        order_spec=cfg.order_by_spec,
        tenant_id=_TENANT,
        relation_columns=relation_columns,
        unique_keys=catalogue.unique_keys,
        not_null_columns=catalogue.not_null_columns,
    )
    assert "newest_dated" in query.sql, "the window is cut from the relation"

    conn = await asyncpg.connect(dsn)
    try:
        explained = await conn.fetchval(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + query.sql, *query.params
        )
    finally:
        await conn.close()

    (root,) = json.loads(explained) if isinstance(explained, str) else explained
    scan = _undated_branch_scan(root["Plan"])
    assert scan["Relation Name"] == table, scan
    assert scan["Actual Loops"] == loops, scan


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
        # 59 s on, the cached answer still holds: the window is still cut
        # straight from the relation and serves both revisions of the key.
        now[0] = 1_059.0
        cached = await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
        assert frozenset({"event_id"}) in _catalogue(source, "live_events").unique_keys
        assert [row["event_id"] for row in cached].count(f"evt-{_ROWS - 1:04d}") == 2
        # 61 s on, the catalogue is read again.
        now[0] = 1_061.0
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
