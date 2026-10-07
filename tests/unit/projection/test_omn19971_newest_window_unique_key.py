# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19971: an exposure without a cursor serves its newest rows, cheaply.

Two defects came in with OMN-20327's reader and were read back on the dev lane
on 2026-10-03:

* the stale window: every read without ``since`` asked the table for an
  ascending ``walk``, including the exposures that declare no
  ``cursor_column``. Their walk can never continue (``since`` is refused for
  them), so they served the newest ``limit`` rows of the OLDEST
  ``limit * 4`` -- ``delegation.decisions`` stopped at 09-26 with 5,938 newer
  rows in the table;
* the timeouts: every keyed exposure went through the recursive latest-row-
  per-key scan, even where the key is unique per row (``work.events``,
  ``session.replay``, ``live-events``), so each read walked the whole table,
  and the page then read the whole window a second time for
  ``latest_event_at``.

Failure modes these tests are written against:

* F6 an exposure with no cursor, read without ``since``, serves the oldest
  window;
* F7 an exposure that declares a cursor no longer starts its walk at the
  oldest row or no longer pages by cursor;
* F8 a key covered by a unique index (or declared immutable) still runs the
  recursive per-key scan, or a mutable key without one stops getting one row
  per key -- including a key whose only unique index is invalid or partial,
  and a key with a nullable column, whose NULL-keyed rows the per-key scan
  never serves;
* F9 one page request runs more than one window query, or a newest window
  serves rows with no recency value ahead of dated ones (Postgres reads
  ``DESC`` as NULLS FIRST), or its ``latest_event_at`` sends a second read
  when the window already answers it;
* F10 the unique-index lookup is interpolated, repeated per request, shared
  between relations, or drops one of the catalogue rules it relies on, or a
  missing relation stops answering ``projection_table_missing``;
* F11 a unique index that compares its key under another collation or
  operator class than the column's own counts as making the key unique per
  row (Postgres prints such an index's key column as the bare column name);
* F12 a unique index dropped while the process runs keeps the fast path on
  once the catalogue's 60 s are up, or a refreshed catalogue answer is not
  cached again;
* F13 the bounded freshness read of a cursor exposure orders the whole
  relation by its freshness column, which no index has to lead
  (``consumer-flow`` on the lab: 42.6M rows, not ordered by ``window_end``
  within 20 s, so every page would have timed out);
* F14 one exposure whose freshness read fails takes the whole status page
  down instead of being named failed beside the others.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.read_page import read_projection_page
from omnimarket.projection.table_reader import (
    DEFAULT_DSN_ENV,
    RETAINED_WINDOW_FACTOR,
    ProjectionReadError,
    TablePageView,
    TableRowSource,
    build_window_query,
)

_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_WORK = "onex.snapshot.projection.work.events.v1"
_LIVE = "onex.snapshot.projection.live-events.v1"
_FLOW = "onex.snapshot.projection.consumer-flow.v1"
_FINGERPRINTS = "onex.snapshot.projection.runtime-error-fingerprints.v1"
_EPOCH = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _decisions_cfg(**overrides: Any) -> ProjectionTableConfig:
    """``delegation.decisions``: no cursor, mutable key, tenant-scoped."""
    columns = ("correlation_id", "tenant_id", "model_name", "written_at")
    fields: dict[str, Any] = {
        "topic": _DECISIONS,
        "table": "delegation_events",
        "schema_name": "public",
        "relation_schema": "public",
        "columns": columns,
        "order_by": "written_at DESC",
        "order_by_spec": parse_order_by_clauses("written_at DESC", columns),
        "freshness_column": "written_at",
        "limit": 5,
        "bus_backed": True,
        "key_columns": ("correlation_id",),
        "key_grain": "mutable",
        "tenant_column": "tenant_id",
    }
    fields.update(overrides)
    return ProjectionTableConfig(**fields)


def _work_cfg() -> ProjectionTableConfig:
    """``work.events``: no cursor, immutable key."""
    columns = ("event_id", "session_id", "emitted_at")
    return ProjectionTableConfig(
        topic=_WORK,
        table="work_events",
        schema_name="omnidash_analytics",
        relation_schema="omninode_internal",
        columns=columns,
        order_by="emitted_at DESC",
        order_by_spec=parse_order_by_clauses("emitted_at DESC", columns),
        freshness_column="emitted_at",
        limit=500,
        bus_backed=True,
        key_columns=("event_id",),
        key_grain="immutable",
    )


def _live_cfg(**overrides: Any) -> ProjectionTableConfig:
    """``live-events``: no cursor, declared mutable, keyed on a unique column."""
    columns = ("event_id", "type", "created_at")
    fields: dict[str, Any] = {
        "topic": _LIVE,
        "table": "live_events",
        "schema_name": "public",
        "relation_schema": "public",
        "columns": columns,
        "order_by": "created_at DESC",
        "order_by_spec": parse_order_by_clauses("created_at DESC", columns),
        "freshness_column": "created_at",
        "limit": 100,
        "bus_backed": True,
        "key_columns": ("event_id",),
        "key_grain": "mutable",
    }
    fields.update(overrides)
    return ProjectionTableConfig(**fields)


def _flow_cfg() -> ProjectionTableConfig:
    """``consumer-flow``: a cursor, a mutable key with many revisions per key."""
    columns = (
        "projection_cursor",
        "consumer_group",
        "topic",
        "window_start",
        "window_end",
    )
    return ProjectionTableConfig(
        topic=_FLOW,
        table="consumer_flow_windows",
        schema_name="omnidash_analytics",
        relation_schema="omninode_internal",
        columns=columns,
        order_by="window_end DESC",
        order_by_spec=parse_order_by_clauses("window_end DESC", columns),
        freshness_column="window_end",
        cursor_column="projection_cursor",
        limit=5,
        bus_backed=True,
        key_columns=("consumer_group", "topic"),
        key_grain="mutable",
        latest_by="window_start",
    )


def _fingerprints_cfg() -> ProjectionTableConfig:
    """``runtime-error-fingerprints``: a cursor, ranked by its declared order."""
    columns = ("projection_cursor", "fingerprint", "occurrences", "last_seen_at")
    return ProjectionTableConfig(
        topic=_FINGERPRINTS,
        table="runtime_error_fingerprints",
        schema_name="omnidash_analytics",
        relation_schema="omninode_internal",
        columns=columns,
        order_by="occurrences DESC",
        order_by_spec=parse_order_by_clauses("occurrences DESC", columns),
        freshness_column="last_seen_at",
        cursor_column="projection_cursor",
        limit=5,
        bus_backed=True,
        key_columns=("fingerprint",),
        key_grain="mutable",
        page_selection="order_by",
    )


def _served_window(cfg: ProjectionTableConfig, order: str) -> str:
    """The tail of the SQL that says which rows the window keeps."""
    return (
        f"ORDER BY {order} LIMIT {cfg.limit * RETAINED_WINDOW_FACTOR}) AS served_window"
    )


def _newest_dated(cfg: ProjectionTableConfig, recency: str) -> str:
    """The tail of the dated branch of a newest window cut from the relation."""
    retain = cfg.limit * RETAINED_WINDOW_FACTOR
    return f'"{recency}" IS NOT NULL ORDER BY "{recency}" DESC LIMIT {retain})'


def _branches(sql: str) -> tuple[str, str]:
    """The dated and the undated branch of a newest window cut from the relation."""
    assert sql.startswith("SELECT * FROM ("), sql
    assert "WITH newest_dated AS (" in sql, sql
    assert " UNION ALL " in sql, sql
    head, undated = sql.split(" UNION ALL ", 1)
    return head.split("WITH newest_dated AS (", 1)[1], undated


def _query(cfg: ProjectionTableConfig, **kwargs: Any) -> str:
    kwargs.setdefault("order_spec", cfg.order_by_spec)
    kwargs.setdefault("tenant_id", _TENANT if cfg.tenant_column else None)
    return build_window_query(cfg, **kwargs).sql


# ---------------------------------------------------------------------------
# F6 / F7: which end of the table the window starts at (SQL shape)
# ---------------------------------------------------------------------------


def test_f6_a_walk_without_a_cursor_column_is_never_built() -> None:
    cfg = _decisions_cfg()
    sql = _query(cfg, selection="walk")
    assert '"written_at" ASC' not in sql, "an ascending window is the oldest rows"
    assert _served_window(cfg, '"written_at" DESC NULLS LAST') in sql


def test_f7_a_cursor_walk_still_starts_at_the_oldest_row() -> None:
    cfg = _flow_cfg()
    sql = _query(cfg, selection="walk")
    assert _served_window(cfg, '"projection_cursor" ASC') in sql


def test_f7_a_since_read_still_walks_up_from_the_cursor() -> None:
    cfg = _flow_cfg()
    query = build_window_query(
        cfg,
        order_spec=(("projection_cursor", "ASC", None),),
        tenant_id=None,
        since="40",
        since_type="bigint",
        selection="walk",
    )
    assert '"projection_cursor" > CAST($1::text AS bigint)' in query.sql
    assert _served_window(cfg, '"projection_cursor" ASC') in query.sql
    assert query.params == ("40",)


# ---------------------------------------------------------------------------
# F8: the recursive per-key scan only where a key can hold several rows
# ---------------------------------------------------------------------------


def test_f8_an_immutable_key_is_read_without_the_per_key_scan() -> None:
    cfg = _work_cfg()
    sql = _query(cfg, relation_columns=frozenset(cfg.columns))
    assert "WITH RECURSIVE" not in sql
    assert (
        'FROM "omninode_internal"."work_events" WHERE '
        + _newest_dated(cfg, "emitted_at")
    ) in sql


def test_f8_a_key_covered_by_a_unique_index_skips_the_per_key_scan() -> None:
    cfg = _live_cfg()
    sql = _query(
        cfg,
        relation_columns=frozenset({"id", *cfg.columns}),
        unique_keys=(frozenset({"id"}), frozenset({"event_id"})),
        not_null_columns=frozenset({"id", *cfg.columns}),
    )
    assert "WITH RECURSIVE" not in sql
    assert (
        'FROM "public"."live_events" WHERE ' + _newest_dated(cfg, "created_at")
    ) in sql


def test_f8_a_unique_index_on_part_of_the_key_still_makes_rows_unique() -> None:
    cfg = _decisions_cfg(key_columns=("tenant_id", "correlation_id"))
    sql = _query(
        cfg,
        relation_columns=frozenset(cfg.columns),
        unique_keys=(frozenset({"correlation_id"}),),
        not_null_columns=frozenset(cfg.columns),
    )
    assert "WITH RECURSIVE" not in sql
    # The tenant scope still applies on the fast path.
    assert 'WHERE "tenant_id"::text = $1 ' in sql


def test_f8_a_mutable_key_without_a_unique_index_keeps_one_row_per_key() -> None:
    cfg = _flow_cfg()
    sql = _query(
        cfg,
        relation_columns=frozenset(cfg.columns),
        unique_keys=(),
        # Every key column NOT NULL, so the nullability rule passes and the
        # missing unique index is what keeps the per-key scan.
        not_null_columns=frozenset(cfg.columns),
    )
    assert "WITH RECURSIVE" in sql
    assert 'ORDER BY t."window_start" DESC LIMIT 1' in sql


def test_f8_a_unique_index_reaching_past_the_key_does_not_count() -> None:
    cfg = _flow_cfg()
    sql = _query(
        cfg,
        relation_columns=frozenset(cfg.columns),
        # The cursor's primary key, and a unique index wider than the key:
        # neither makes (consumer_group, topic) unique per row.
        unique_keys=(
            frozenset({"projection_cursor"}),
            frozenset({"consumer_group", "topic", "window_start"}),
        ),
        # Every key column NOT NULL, so the nullability rule passes and the
        # index's reach past the key is what keeps the per-key scan.
        not_null_columns=frozenset(cfg.columns),
    )
    assert "WITH RECURSIVE" in sql


# ---------------------------------------------------------------------------
# F9: a newest window serves its dated rows first (SQL shape)
# ---------------------------------------------------------------------------


def _direct_decisions(
    route: str,
) -> tuple[ProjectionTableConfig, dict[str, Any]]:
    """``delegation.decisions`` with a key unique per row, so its window is cut
    straight from the relation: by its declared grain, or by a unique index."""
    if route == "immutable_grain":
        return _decisions_cfg(key_grain="immutable"), {}
    cfg = _decisions_cfg()
    return cfg, {
        "relation_columns": frozenset(cfg.columns),
        "unique_keys": (frozenset({"correlation_id"}),),
        "not_null_columns": frozenset(cfg.columns),
    }


@pytest.mark.parametrize("route", ["immutable_grain", "unique_index"])
def test_f9_a_newest_window_cut_from_the_relation_serves_dated_rows_first(
    route: str,
) -> None:
    # Postgres reads DESC as NULLS FIRST, so ORDER BY written_at DESC LIMIT n
    # fills the window with undated rows before any dated one. The dated rows
    # come first, through the column's index, and undated rows only fill what
    # they leave of the window.
    cfg, catalogue = _direct_decisions(route)
    retain = cfg.limit * RETAINED_WINDOW_FACTOR
    query = build_window_query(
        cfg, order_spec=cfg.order_by_spec, tenant_id=_TENANT, **catalogue
    )

    assert "WITH RECURSIVE" not in query.sql
    dated, undated = _branches(query.sql)
    assert _newest_dated(cfg, "written_at") in dated
    assert (
        f'"written_at" IS NULL LIMIT {retain} - (SELECT count(*) FROM newest_dated)'
    ) in undated
    # The tenant scope is in both branches, bound once.
    assert 'WHERE "tenant_id"::text = $1 AND ' in dated
    assert 'WHERE "tenant_id"::text = $1 AND ' in undated
    assert _TENANT not in query.sql
    assert query.params == (_TENANT,)
    assert query.sql.endswith(
        ') AS served_window ORDER BY "written_at" DESC NULLS LAST'
    )


def test_f9_a_correlation_filter_applies_to_both_branches_of_the_newest_window() -> (
    None
):
    cfg, catalogue = _direct_decisions("unique_index")
    query = build_window_query(
        cfg,
        order_spec=cfg.order_by_spec,
        tenant_id=_TENANT,
        correlation_id="corr-00003",
        **catalogue,
    )

    dated, undated = _branches(query.sql)
    scope = 'WHERE "tenant_id"::text = $1 AND "correlation_id"::text = $2 AND '
    assert scope + '"written_at" IS NOT NULL' in dated
    assert scope + '"written_at" IS NULL' in undated
    assert query.params == (_TENANT, "corr-00003")


def test_f9_a_cursor_exposure_cut_from_the_relation_serves_dated_rows_first() -> None:
    # An exposure with a cursor whose key is unique per row is cut straight
    # from the relation too, and its newest window is the same two branches
    # over the cursor.
    cfg = _flow_cfg()
    retain = cfg.limit * RETAINED_WINDOW_FACTOR
    sql = _query(
        cfg,
        relation_columns=frozenset(cfg.columns),
        unique_keys=(frozenset({"consumer_group", "topic"}),),
        not_null_columns=frozenset(cfg.columns),
    )

    assert "WITH RECURSIVE" not in sql
    dated, undated = _branches(sql)
    assert _newest_dated(cfg, "projection_cursor") in dated
    assert (
        f'"projection_cursor" IS NULL LIMIT {retain} - '
        "(SELECT count(*) FROM newest_dated)"
    ) in undated
    assert sql.endswith(') AS served_window ORDER BY "window_end" DESC NULLS LAST')


def test_f9_the_latest_row_per_key_window_puts_undated_rows_last() -> None:
    # The per-key rows are sorted in memory whatever the NULLS clause, so the
    # clause costs nothing here.
    cfg = _decisions_cfg()
    sql = _query(
        cfg,
        relation_columns=frozenset(cfg.columns),
        not_null_columns=frozenset(cfg.columns),
    )
    assert "WITH RECURSIVE" in sql
    assert ") AS latest " + _served_window(cfg, '"written_at" DESC NULLS LAST') in sql


def test_f9_only_a_newest_window_by_recency_changes_its_window_order() -> None:
    # A walk, a since read, a ranked read and the newest window of an exposure
    # with no recency column keep the window order they had.
    flow = _flow_cfg()
    flow_unique = {
        "relation_columns": frozenset(flow.columns),
        "unique_keys": (frozenset({"consumer_group", "topic"}),),
        "not_null_columns": frozenset(flow.columns),
    }
    since_read = build_window_query(
        flow,
        order_spec=(("projection_cursor", "ASC", None),),
        tenant_id=None,
        since="40",
        since_type="bigint",
        **flow_unique,
    ).sql
    fingerprints = _fingerprints_cfg()
    undated = _live_cfg(freshness_column=None)
    cases = {
        "keyed walk": (flow, _query(flow, selection="walk"), '"projection_cursor" ASC'),
        "direct walk": (
            flow,
            _query(flow, selection="walk", **flow_unique),
            '"projection_cursor" ASC',
        ),
        "since read": (flow, since_read, '"projection_cursor" ASC'),
        "ranked read": (
            fingerprints,
            _query(fingerprints, selection="ranked"),
            '"occurrences" DESC NULLS LAST',
        ),
        "no recency column": (undated, _query(undated), '"created_at" DESC NULLS LAST'),
    }
    for name, (cfg, sql, order) in cases.items():
        assert "newest_dated" not in sql, name
        assert _served_window(cfg, order) in sql, (name, sql)


# ---------------------------------------------------------------------------
# F6 / F7 through the route, over a source that selects like the table does
# ---------------------------------------------------------------------------


def _nulls_last(
    rows: list[dict[str, Any]], column: str, *, descending: bool
) -> list[dict[str, Any]]:
    """``rows`` stably sorted by ``column``, rows without a value last."""
    present = [row for row in rows if row[column] is not None]
    absent = [row for row in rows if row[column] is None]
    return [*sorted(present, key=lambda r: r[column], reverse=descending), *absent]


class _WindowedSource:
    """A row source that keeps ``limit * 4`` rows the way the table reader does.

    ``newest`` keeps the newest rows by the recency column, ``walk`` the
    oldest, ``ranked`` the top of the declared order; ``since`` keeps the
    rows above the cursor. A row with no value in a sorted column comes last,
    as the real sources serve it. ``latest_event_at`` reads the newest-rows window
    again when it is not handed one, as both real sources do.
    """

    backing = "table"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.window_reads: list[str] = []

    def unavailable(self, topic: str) -> tuple[str, str] | None:
        return None

    async def rows(
        self,
        cfg: ProjectionTableConfig,
        *,
        order_spec: tuple[tuple[str, str, str | None], ...],
        tenant_id: str | None,
        since: str | None = None,
        correlation_id: str | None = None,
        ticket_id: str | None = None,
        selection: str = "newest",
    ) -> list[dict[str, Any]]:
        self.window_reads.append(selection)
        recency = cfg.cursor_column or cfg.freshness_column
        assert recency is not None
        rows = [
            r for r in self._rows if tenant_id is None or r["tenant_id"] == tenant_id
        ]
        if correlation_id is not None:
            # The table reader filters in its WHERE clause, before the window.
            rows = [r for r in rows if r["correlation_id"] == correlation_id]
        if ticket_id is not None:
            rows = [r for r in rows if r["ticket_id"] == ticket_id]
        if since is not None:
            assert cfg.cursor_column is not None
            rows = [r for r in rows if r[cfg.cursor_column] > int(since)]
        ascending = since is not None or selection == "walk"
        rows = _nulls_last(rows, recency, descending=not ascending)
        window = rows[: cfg.limit * RETAINED_WINDOW_FACTOR]
        for column, direction, _nulls in reversed(order_spec):
            window = _nulls_last(window, column, descending=direction == "DESC")
        return window

    async def walk_origin(
        self, cfg: ProjectionTableConfig, *, tenant_id: str | None
    ) -> str | None:
        return None

    async def latest_event_at(
        self,
        cfg: ProjectionTableConfig,
        *,
        tenant_id: str | None,
        window_rows: list[dict[str, Any]] | None = None,
    ) -> datetime | None:
        assert cfg.freshness_column is not None
        rows = window_rows
        if rows is None:
            rows = await self.rows(cfg, order_spec=(), tenant_id=tenant_id)
        values = [row[cfg.freshness_column] for row in rows]
        return max((value for value in values if value is not None), default=None)

    def staleness(self, topic: str, latest_ts: str | None) -> dict[str, object]:
        return {}

    async def page_view(
        self,
        topic_map: dict[str, ProjectionTableConfig],
        *,
        tenant_id: str | None,
    ) -> TablePageView:
        raise NotImplementedError

    async def readiness(
        self, topic_map: dict[str, ProjectionTableConfig]
    ) -> tuple[bool, dict[str, object]]:
        raise NotImplementedError

    def health(self, topic_map: dict[str, ProjectionTableConfig]) -> dict[str, object]:
        raise NotImplementedError


def _decision_rows(count: int) -> list[dict[str, Any]]:
    return [
        {
            "correlation_id": f"corr-{index:05d}",
            "tenant_id": _TENANT,
            "model_name": "qwen",
            "written_at": _EPOCH + timedelta(minutes=index),
        }
        for index in range(count)
    ]


async def test_f6_a_page_without_a_cursor_serves_the_newest_rows() -> None:
    cfg = _decisions_cfg()
    retain = cfg.limit * RETAINED_WINDOW_FACTOR
    table = _decision_rows(retain * 3)
    newest = max(row["written_at"] for row in table)
    source = _WindowedSource(table)

    page = await read_projection_page(
        _DECISIONS, topic_map={_DECISIONS: cfg}, source=source, tenant=_TENANT
    )

    assert page.status_code == 200, page.body
    served = [row["written_at"] for row in page.body["rows"]]
    assert served[0] == newest, "the newest row of the table is on page one"
    assert (
        served
        == sorted((row["written_at"] for row in table), reverse=True)[: cfg.limit]
    )
    assert page.body["latest_event_at"] == newest.isoformat()
    assert page.body["next_cursor"] is None


async def test_f9_a_correlation_filtered_page_reports_the_exposures_newest() -> None:
    # The filtered window holds only the asked-for rows, so it cannot stand in
    # for the exposure's newest value: the page must report the newest row of
    # the exposure, as it did before the window was reused.
    cfg = _decisions_cfg()
    table = _decision_rows(cfg.limit * RETAINED_WINDOW_FACTOR * 2)
    newest = max(row["written_at"] for row in table)
    asked = table[3]
    source = _WindowedSource(table)

    page = await read_projection_page(
        _DECISIONS,
        topic_map={_DECISIONS: cfg},
        source=source,
        tenant=_TENANT,
        correlation_id=asked["correlation_id"],
    )

    assert page.status_code == 200, page.body
    assert [row["correlation_id"] for row in page.body["rows"]] == [
        asked["correlation_id"]
    ]
    assert page.body["latest_event_at"] == newest.isoformat()


async def test_f7_a_page_with_a_cursor_starts_the_walk_and_pages_by_cursor() -> None:
    cfg = _flow_cfg()
    table = [
        {
            "projection_cursor": cursor,
            "consumer_group": f"group-{cursor:03d}",
            "topic": "t",
            "window_start": _EPOCH + timedelta(minutes=cursor),
            "window_end": _EPOCH + timedelta(minutes=cursor + 1),
        }
        for cursor in range(1, 61)
    ]
    source = _WindowedSource(table)

    first = await read_projection_page(_FLOW, topic_map={_FLOW: cfg}, source=source)
    assert first.status_code == 200, first.body
    assert sorted(r["projection_cursor"] for r in first.body["rows"]) == [1, 2, 3, 4, 5]
    assert first.body["next_cursor"] == "5"
    assert source.window_reads[0] == "walk"

    second = await read_projection_page(
        _FLOW, topic_map={_FLOW: cfg}, source=source, since=first.body["next_cursor"]
    )
    assert second.status_code == 200, second.body
    assert sorted(r["projection_cursor"] for r in second.body["rows"]) == [
        6,
        7,
        8,
        9,
        10,
    ]
    assert second.body["next_cursor"] == "10"


# ---------------------------------------------------------------------------
# F9 / F10: the Postgres row source over a fake connection
# ---------------------------------------------------------------------------


def _postgres_newest(
    sql: str, column: str, values: list[datetime | None]
) -> datetime | None:
    """What Postgres answers to ``SELECT col ... ORDER BY col DESC LIMIT 1``.

    ``DESC`` sorts NULLs first unless the query states ``NULLS LAST``, and a
    ``col IS NOT NULL`` predicate leaves them out.
    """
    quoted = f'"{column}"'
    assert f"ORDER BY {quoted} DESC" in sql, sql
    assert sql.endswith("LIMIT 1"), sql
    if f"{quoted} IS NOT NULL" in sql:
        values = [value for value in values if value is not None]
    present = sorted((value for value in values if value is not None), reverse=True)
    nulls = [value for value in values if value is None]
    if f"{quoted} DESC NULLS LAST" in sql:
        ordered = [*present, *nulls]
    else:
        ordered = [*nulls, *present]
    return ordered[0] if ordered else None


class _Connection:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    @asynccontextmanager
    async def transaction(self, *, readonly: bool = False) -> Any:
        yield

    async def execute(self, sql: str, *params: Any) -> str:
        self._pool.log.append((sql, params))
        return "OK"

    async def fetchval(self, sql: str, *params: Any) -> Any:
        self._pool.log.append((sql, params))
        if "pg_attribute" in sql:
            return self._pool.relation_columns
        if self._pool.freshness is not None:
            return _postgres_newest(sql, *self._pool.freshness)
        return self._pool.newest

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self._pool.log.append((sql, params))
        if "pg_index" in sql:
            # One catalogue row per unique index, as the lookup selects them,
            # of the relation the lookup is bound to when that is given.
            not_null = [
                column
                for column in self._pool.relation_columns or []
                if column not in self._pool.nullable
            ]
            unique_keys = self._pool.unique_keys
            if self._pool.unique_keys_by_relation is not None:
                unique_keys = self._pool.unique_keys_by_relation[params[0]]
            return [
                {
                    "key_columns": list(index),
                    "is_valid": index not in self._pool.invalid,
                    "is_partial": index in self._pool.partial,
                    "plain_equality": index not in self._pool.other_equality,
                    "not_null_columns": not_null,
                }
                for index in unique_keys
            ]
        if self._pool.missing:
            raise asyncpg.UndefinedTableError("relation does not exist")
        return [dict(row) for row in self._pool.window]


class _Pool:
    def __init__(
        self,
        *,
        relation_columns: list[str] | None,
        window: list[dict[str, Any]] | None = None,
        unique_keys: tuple[tuple[str, ...], ...] = (),
        unique_keys_by_relation: dict[str, tuple[tuple[str, ...], ...]] | None = None,
        nullable: frozenset[str] = frozenset(),
        invalid: frozenset[tuple[str, ...]] = frozenset(),
        partial: frozenset[tuple[str, ...]] = frozenset(),
        other_equality: frozenset[tuple[str, ...]] = frozenset(),
        newest: datetime | None = None,
        freshness: tuple[str, list[datetime | None]] | None = None,
        missing: bool = False,
    ) -> None:
        # ``freshness`` is (column, its values in the relation): the bounded
        # newest-value read is then answered the way Postgres orders them.
        # ``other_equality`` are the unique indexes whose collation or
        # operator class is not their column's own. ``unique_keys_by_relation``
        # answers the catalogue per relation, by the lookup's bound relation.
        self.freshness = freshness
        self.relation_columns = relation_columns
        self.window = window or []
        self.unique_keys = unique_keys
        self.unique_keys_by_relation = unique_keys_by_relation
        self.nullable = nullable
        self.invalid = invalid
        self.partial = partial
        self.other_equality = other_equality
        self.newest = newest
        self.missing = missing
        self.log: list[tuple[str, tuple[Any, ...]]] = []

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield _Connection(self)

    def window_queries(self) -> list[str]:
        return [sql for sql, _ in self.log if sql.startswith("SELECT * FROM (")]


def _source(pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> TableRowSource:
    source = TableRowSource(environ={DEFAULT_DSN_ENV: "postgresql://unused/db"})

    async def _pool(cfg: ProjectionTableConfig) -> Any:
        return pool

    monkeypatch.setattr(source, "_pool", _pool)
    return source


async def test_f9_a_page_without_a_cursor_runs_one_window_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _decisions_cfg()
    window = _decision_rows(7)
    pool = _Pool(relation_columns=list(cfg.columns), window=window)
    source = _source(pool, monkeypatch)

    page = await read_projection_page(
        _DECISIONS, topic_map={_DECISIONS: cfg}, source=source, tenant=_TENANT
    )

    assert page.status_code == 200, page.body
    assert len(pool.window_queries()) == 1
    newest = max(row["written_at"] for row in window)
    assert page.body["latest_event_at"] == newest.isoformat()
    # The one window read is the newest one (F6 at the SQL the route sends).
    assert (
        _served_window(cfg, '"written_at" DESC NULLS LAST') in pool.window_queries()[0]
    )


async def test_f9_a_cursor_walk_page_runs_one_window_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _flow_cfg()
    newest = _EPOCH + timedelta(days=3)
    window = [
        {
            "projection_cursor": cursor,
            "consumer_group": f"g{cursor}",
            "topic": "t",
            "window_start": _EPOCH,
            "window_end": _EPOCH + timedelta(minutes=cursor),
        }
        for cursor in range(1, 4)
    ]
    pool = _Pool(relation_columns=list(cfg.columns), window=window, newest=newest)
    source = _source(pool, monkeypatch)

    page = await read_projection_page(_FLOW, topic_map={_FLOW: cfg}, source=source)

    assert page.status_code == 200, page.body
    assert len(pool.window_queries()) == 1
    # The walk's rows are the oldest, so the freshness comes from a bounded
    # read of the newest value, not from the rows the walk served.
    assert page.body["latest_event_at"] == newest.isoformat()
    bounded = [sql for sql, _ in pool.log if sql.startswith("SELECT max(")]
    assert len(bounded) == 1, "the newest value is read with one bounded query"


async def test_f13_a_cursor_exposures_freshness_read_follows_its_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cursor exposure is ordered by its cursor, and nothing makes its
    # freshness column the leading column of an index. Its newest freshness
    # value is read from the newest limit * 4 rows by the cursor -- the index
    # the walk itself uses -- never by ordering the relation on the freshness
    # column.
    cfg = _flow_cfg()
    newest = _EPOCH + timedelta(days=3)
    pool = _Pool(relation_columns=list(cfg.columns), newest=newest)
    source = _source(pool, monkeypatch)

    assert await source.latest_event_at(cfg, tenant_id=None) == newest

    assert pool.window_queries() == [], "no window is read for the freshness"
    reads = [(sql, params) for sql, params in pool.log if '"window_end"' in sql]
    assert len(reads) == 1
    sql, params = reads[0]
    retain = cfg.limit * RETAINED_WINDOW_FACTOR
    assert sql.startswith('SELECT max("window_end") FROM (SELECT "window_end" FROM ')
    assert sql.endswith(f'ORDER BY "projection_cursor" DESC LIMIT {retain}) AS newest')
    assert 'ORDER BY "window_end"' not in sql
    assert " WHERE " not in sql, "an unscoped exposure reads without a filter"
    assert params == ()


async def test_f13_a_tenant_scoped_cursor_exposure_reads_its_own_tenants_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    columns = ("written_seq", "correlation_id", "tenant_id", "written_at")
    cfg = _decisions_cfg(columns=columns, cursor_column="written_seq")
    newest = _EPOCH + timedelta(days=2)
    pool = _Pool(relation_columns=list(columns), newest=newest)
    source = _source(pool, monkeypatch)

    assert await source.latest_event_at(cfg, tenant_id=_TENANT) == newest

    reads = [(sql, params) for sql, params in pool.log if '"written_at"' in sql]
    assert len(reads) == 1
    sql, params = reads[0]
    retain = cfg.limit * RETAINED_WINDOW_FACTOR
    # The tenant filter is inside the bounded read, as a bound value: the
    # newest rows by cursor are the tenant's own.
    assert _TENANT not in sql
    assert params == (_TENANT,)
    assert sql.endswith(
        ' WHERE "tenant_id"::text = $1'
        f' ORDER BY "written_seq" DESC LIMIT {retain}) AS newest'
    )
    with pytest.raises(ProjectionReadError) as refused:
        await source.latest_event_at(cfg, tenant_id=None)
    assert refused.value.code == "tenant_context_unresolved"


async def test_f9_a_ranked_page_runs_one_window_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _fingerprints_cfg()
    newest = _EPOCH + timedelta(days=1)
    pool = _Pool(relation_columns=list(cfg.columns), window=[], newest=newest)
    source = _source(pool, monkeypatch)

    page = await read_projection_page(
        _FINGERPRINTS, topic_map={_FINGERPRINTS: cfg}, source=source
    )

    assert page.status_code == 200, page.body
    assert len(pool.window_queries()) == 1
    assert page.body["latest_event_at"] == newest.isoformat()


async def test_f9_the_bounded_freshness_read_binds_the_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _decisions_cfg()
    newest = _EPOCH + timedelta(days=2)
    pool = _Pool(relation_columns=list(cfg.columns), newest=newest)
    source = _source(pool, monkeypatch)

    latest = await source.latest_event_at(cfg, tenant_id=_TENANT)

    assert latest == newest
    assert pool.window_queries() == [], "no window is read for the freshness"
    reads = [(sql, params) for sql, params in pool.log if '"written_at"' in sql]
    assert len(reads) == 1
    sql, params = reads[0]
    assert _TENANT not in sql
    assert params == (_TENANT,)
    # The newest value is the tenant's own, never another tenant's.
    assert ' WHERE "tenant_id"::text = $1 AND ' in sql
    assert sql.endswith('ORDER BY "written_at" DESC LIMIT 1')


async def test_f9_a_null_freshness_value_does_not_hide_the_newest_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ORDER BY col DESC puts NULLs first, so without a NULL guard one row with
    # no freshness value would make the exposure's latest_event_at None.
    cfg = _decisions_cfg()
    newest = _EPOCH + timedelta(days=2)
    pool = _Pool(
        relation_columns=list(cfg.columns),
        freshness=("written_at", [_EPOCH, None, newest]),
    )
    source = _source(pool, monkeypatch)

    assert await source.latest_event_at(cfg, tenant_id=_TENANT) == newest


def _undated_rows(count: int) -> list[dict[str, Any]]:
    return [
        {**row, "correlation_id": f"undated-{index}", "written_at": None}
        for index, row in enumerate(_decision_rows(count))
    ]


async def test_f9_a_window_with_undated_rows_reports_its_newest_dated_value_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The newest window serves dated rows first, so its newest dated value is
    # the exposure's newest, undated rows beside it or not. The relation would
    # answer a later value if it were asked, so an answer from a second read
    # shows, as does any statement sent.
    cfg = _decisions_cfg()
    dated = _decision_rows(4)
    pool = _Pool(
        relation_columns=list(cfg.columns),
        freshness=("written_at", [_EPOCH + timedelta(days=9), None]),
    )
    source = _source(pool, monkeypatch)

    latest = await source.latest_event_at(
        cfg, tenant_id=_TENANT, window_rows=[*dated, *_undated_rows(3)]
    )

    assert latest == max(row["written_at"] for row in dated)
    assert pool.log == [], "the window answers; nothing is read"


async def test_f9_a_window_of_only_undated_rows_reports_no_latest_value_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Undated rows enter the newest window only when the scope has no further
    # dated row, so a window holding only undated rows means the scope has no
    # dated row at all: there is no value, and nothing to read.
    cfg = _decisions_cfg()
    pool = _Pool(
        relation_columns=list(cfg.columns),
        freshness=("written_at", [_EPOCH, None]),
    )
    source = _source(pool, monkeypatch)

    latest = await source.latest_event_at(
        cfg,
        tenant_id=_TENANT,
        window_rows=_undated_rows(cfg.limit * RETAINED_WINDOW_FACTOR),
    )

    assert latest is None
    assert pool.log == [], "the window answers; nothing is read"


async def test_f9_an_empty_window_asks_for_no_bounded_freshness_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The unfiltered newest window is empty only when the scope holds no row
    # at all, so there is no value to read.
    cfg = _decisions_cfg()
    pool = _Pool(
        relation_columns=list(cfg.columns),
        window=[],
        freshness=("written_at", []),
    )
    source = _source(pool, monkeypatch)

    page = await read_projection_page(
        _DECISIONS, topic_map={_DECISIONS: cfg}, source=source, tenant=_TENANT
    )

    assert page.status_code == 200, page.body
    assert page.body["latest_event_at"] is None
    assert [sql for sql, _ in pool.log if sql.endswith("DESC LIMIT 1")] == []


async def test_f10_the_unique_index_lookup_is_bound_and_read_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _live_cfg()
    pool = _Pool(
        relation_columns=["id", *cfg.columns],
        unique_keys=(("id",), ("event_id",)),
    )
    source = _source(pool, monkeypatch)

    for _ in range(2):
        await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)

    lookups = [(sql, params) for sql, params in pool.log if "pg_index" in sql]
    assert len(lookups) == 1, "the relation's unique indexes are read once, then cached"
    sql, params = lookups[0]
    assert "to_regclass($1)" in sql
    assert "live_events" not in sql, "the relation is a bound value"
    assert params == ('"public"."live_events"',)
    reads = pool.window_queries()
    assert len(reads) == 2
    assert all("WITH RECURSIVE" not in read for read in reads)


async def test_f10_the_unique_index_lookup_states_every_rule_it_relies_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fake catalogue answers whatever statement it is sent, so the rules
    # that decide whether an index counts are checked on the statement itself.
    cfg = _live_cfg()
    pool = _Pool(relation_columns=["id", *cfg.columns], unique_keys=(("event_id",),))
    source = _source(pool, monkeypatch)

    await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)

    ((sql, params),) = [(sql, params) for sql, params in pool.log if "pg_index" in sql]
    rules = {
        "AND i.indisunique": "only a unique index makes a key unique",
        "i.indexprs IS NULL": "an expression column is not the plain column",
        "generate_series(1, i.indnkeyatts) AS k ORDER BY k) AS key_columns": (
            "INCLUDE columns take no part in uniqueness"
        ),
        "i.indisvalid AS is_valid": "an invalid index enforces nothing",
        "i.indpred IS NOT NULL AS is_partial": "a predicate exempts other rows",
        "AND c.is_nullable = 'NO') AS not_null_columns": "NULL keys repeat",
        "a.attcollation = i.indcollation[k - 1]": "the column's own collation",
        "AND o.opcdefault": "the type's default operator class",
        "WHERE i.indrelid = to_regclass($1)": "the relation is a bound value",
    }
    assert {rule: why for rule, why in rules.items() if rule not in sql} == {}
    assert params == ('"public"."live_events"',)


async def test_f10_each_relation_keeps_its_own_unique_index_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The catalogue answer is cached per relation. Two exposures over tables
    # of one name in two schemas, read through one source: only the first has
    # a unique index over the key, so the second still needs the per-key scan.
    unique = _live_cfg()
    mutable = _live_cfg(
        topic="onex.snapshot.projection.live-events-internal.v1",
        relation_schema="omninode_internal",
    )
    pool = _Pool(
        relation_columns=["id", *unique.columns],
        unique_keys_by_relation={
            '"public"."live_events"': (("id",), ("event_id",)),
            '"omninode_internal"."live_events"': (("id",),),
        },
    )
    source = _source(pool, monkeypatch)

    for cfg in (unique, mutable):
        await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)

    first, second = pool.window_queries()
    assert "WITH RECURSIVE" not in first
    assert "WITH RECURSIVE" in second, "the second relation's own catalogue answers"
    assert [params for sql, params in pool.log if "pg_index" in sql] == [
        ('"public"."live_events"',),
        ('"omninode_internal"."live_events"',),
    ]


async def _window_read(
    cfg: ProjectionTableConfig, pool: _Pool, monkeypatch: pytest.MonkeyPatch
) -> str:
    """The one window query the row source sends for ``cfg``."""
    source = _source(pool, monkeypatch)
    await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
    (read,) = pool.window_queries()
    return read


async def test_f8_a_unique_index_over_a_nullable_key_keeps_the_per_key_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A unique index lets any number of rows hold a NULL key, and the per-key
    # scan never serves such a row (its key matches no key by equality). Cut
    # straight from the relation, the window would serve them.
    cfg = _live_cfg()
    pool = _Pool(
        relation_columns=["id", *cfg.columns],
        unique_keys=(("id",), ("event_id",)),
        nullable=frozenset({"event_id"}),
    )

    assert "WITH RECURSIVE" in await _window_read(cfg, pool, monkeypatch)


async def test_f8_a_nullable_key_column_outside_the_unique_index_keeps_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The index makes the key unique, but a NULL in the key's other column
    # still keeps a row out of the per-key scan.
    cfg = _live_cfg(key_columns=("type", "event_id"))
    pool = _Pool(
        relation_columns=["id", *cfg.columns],
        unique_keys=(("event_id",),),
        nullable=frozenset({"type"}),
    )

    assert "WITH RECURSIVE" in await _window_read(cfg, pool, monkeypatch)


async def test_f8_an_invalid_unique_index_keeps_the_per_key_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failed CREATE UNIQUE INDEX CONCURRENTLY leaves the index behind,
    # marked invalid: it enforces nothing, so a key still holds many rows.
    cfg = _flow_cfg()
    key = ("consumer_group", "topic")
    pool = _Pool(
        relation_columns=list(cfg.columns),
        unique_keys=(("projection_cursor",), key),
        invalid=frozenset({key}),
    )

    assert "WITH RECURSIVE" in await _window_read(cfg, pool, monkeypatch)


async def test_f8_a_partial_unique_index_keeps_the_per_key_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A partial unique index binds only the rows its predicate selects; every
    # other row of a key is free to repeat it.
    cfg = _flow_cfg()
    key = ("consumer_group", "topic")
    pool = _Pool(
        relation_columns=list(cfg.columns),
        unique_keys=(("projection_cursor",), key),
        partial=frozenset({key}),
    )

    assert "WITH RECURSIVE" in await _window_read(cfg, pool, monkeypatch)


async def test_f8_a_valid_full_unique_index_on_the_same_key_skips_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The control for the two above: the same key and index, valid and full.
    cfg = _flow_cfg()
    pool = _Pool(
        relation_columns=list(cfg.columns),
        unique_keys=(("projection_cursor",), ("consumer_group", "topic")),
    )

    assert "WITH RECURSIVE" not in await _window_read(cfg, pool, monkeypatch)


async def test_f11_a_unique_index_under_another_equality_keeps_the_per_key_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A unique index forbids two rows that are equal under ITS collation and
    # operator class. The per-key scan compares with the column's own: under a
    # case-insensitive column collation, a "C"-collated unique index admits
    # 'A' and 'a', which the scan serves as one key and a window cut straight
    # from the relation would serve as two. The control is the valid, full
    # index on the same key above.
    cfg = _flow_cfg()
    key = ("consumer_group", "topic")
    pool = _Pool(
        relation_columns=list(cfg.columns),
        unique_keys=(("projection_cursor",), key),
        other_equality=frozenset({key}),
    )

    assert "WITH RECURSIVE" in await _window_read(cfg, pool, monkeypatch)


async def test_f12_a_dropped_unique_index_stops_the_fast_path_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The catalogue answer is cached, but not for the life of the process: a
    # unique index dropped by a migration (or left invalid by a failed REINDEX
    # CONCURRENTLY) must stop the fast path, or a mutable key would serve
    # every revision of a key until the next restart.
    cfg = _flow_cfg()
    key = ("consumer_group", "topic")
    pool = _Pool(
        relation_columns=list(cfg.columns),
        unique_keys=(("projection_cursor",), key),
    )
    source = _source(pool, monkeypatch)
    now = [1_000.0]
    monkeypatch.setattr(source, "_clock", lambda: now[0], raising=False)

    async def read() -> str:
        await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
        return pool.window_queries()[-1]

    def lookups() -> int:
        return len([sql for sql, _ in pool.log if "pg_index" in sql])

    assert "WITH RECURSIVE" not in await read()
    pool.unique_keys = (("projection_cursor",),)  # the key's index is dropped
    now[0] = 1_059.0
    assert "WITH RECURSIVE" not in await read(), "59 s later it is still cached"
    assert lookups() == 1
    now[0] = 1_061.0
    assert "WITH RECURSIVE" in await read(), "61 s later the catalogue is read again"
    assert lookups() == 2
    now[0] = 1_062.0
    assert "WITH RECURSIVE" in await read()
    assert lookups() == 2, "the refreshed answer is cached again"
    now[0] = 1_121.0
    await read()
    assert lookups() == 3, "60 s after the refresh the catalogue is read again"


def test_f8_unknown_key_nullability_keeps_the_per_key_scan() -> None:
    # Without the relation's NOT NULL columns the fast path cannot be shown
    # to serve the same rows, so it is not taken.
    cfg = _live_cfg()
    sql = _query(
        cfg,
        relation_columns=frozenset(cfg.columns),
        unique_keys=(frozenset({"event_id"}),),
    )
    assert "WITH RECURSIVE" in sql


async def test_f10_a_missing_relation_still_answers_table_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _live_cfg(table="never_written")
    pool = _Pool(relation_columns=None, missing=True)
    source = _source(pool, monkeypatch)

    for _ in range(2):
        with pytest.raises(ProjectionReadError) as refused:
            await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=None)
        assert refused.value.code == "projection_table_missing"

    # Nothing about a relation that does not exist is cached, so it is served
    # as soon as its writer creates it.
    column_lookups = [sql for sql, _ in pool.log if "pg_attribute" in sql]
    assert len(column_lookups) == 2


# ---------------------------------------------------------------------------
# F14: the status page names a failed exposure and renders the rest
# ---------------------------------------------------------------------------


async def test_f14_a_failed_freshness_read_fails_only_its_own_exposure_on_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The status page answers 200 with per-panel health: an error reading one
    # exposure's newest value names that exposure failed, and every other
    # exposure still renders.
    broken = _decisions_cfg()
    working = _decisions_cfg(
        topic="onex.snapshot.projection.delegation.decisions-copy.v1",
        table="delegation_events_copy",
    )
    window = _decision_rows(3)
    pool = _Pool(relation_columns=list(broken.columns), window=window)
    source = _source(pool, monkeypatch)
    read_latest = source.latest_event_at

    async def latest_event_at(
        cfg: ProjectionTableConfig,
        *,
        tenant_id: str | None,
        window_rows: list[dict[str, Any]] | None = None,
    ) -> datetime | None:
        if cfg.topic == broken.topic:
            raise ProjectionReadError(
                "projection_database_unavailable", "reading the relation failed"
            )
        return await read_latest(cfg, tenant_id=tenant_id, window_rows=window_rows)

    monkeypatch.setattr(source, "latest_event_at", latest_event_at)

    view = await source.page_view(
        {broken.topic: broken, working.topic: working}, tenant_id=_TENANT
    )

    assert view.failures == {
        broken.topic: ("projection_database_unavailable", "reading the relation failed")
    }
    assert broken.topic not in view.rows
    assert view.unavailable_reason(broken.topic) is not None
    assert [row["correlation_id"] for row in view.rows[working.topic]] == [
        row["correlation_id"] for row in window
    ]
    assert view.latest[working.topic] == max(row["written_at"] for row in window)
    assert view.unavailable_reason(working.topic) is None
