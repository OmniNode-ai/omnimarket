# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read node's row source over the local SQLite store (OMN-20329).

Local MVP mode 1 runs the ``onex`` process with an in-process bus and writes
its projection rows into a SQLite file (``sqlite_database.SqliteDatabaseAdapter``),
not into Postgres. Decision D1 (a) routes the local dashboard's reads through
the runtime read node (OMN-20159), so that node needs a row source over the
same file. This is it: the same served window, tenant scoping, row shape and
named refusals as :class:`~omnimarket.projection.table_reader.TableRowSource`,
in SQLite's dialect.

What differs from the Postgres reader, and why:

* SQLite has no schemas. The local writers create every table in the file's
  main database, so the relation is the bare table name and the exposure's
  ``relation_schema`` is ignored.
* There is no row-level security to agree with, so the tenant is enforced by
  the WHERE clause alone.
* A ``since`` walk compares the cursor column with the bound text directly.
  The local writers store timestamps as ISO strings, which order as text.
* The file is opened read-only per read, and a missing file is a named refusal:
  a read never creates the store it was pointed at.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import (
    RETAINED_WINDOW_FACTOR,
    ProjectionReadError,
    TablePageView,
    WindowQuery,
    _parse_timestamp,
    order_clause,
    quote_identifier,
    recency_column,
    select_list,
    serialise_row,
)


def build_sqlite_window_query(
    cfg: ProjectionTableConfig,
    *,
    order_spec: tuple[tuple[str, str, str | None], ...],
    tenant_id: str | None,
    since: str | None = None,
    correlation_id: str | None = None,
) -> WindowQuery:
    """The SQLite SQL for one exposure's served window.

    The window is the one :func:`table_reader.build_window_query` reads: the
    newest ``limit * 4`` rows by the recency column, or the next ``limit * 4``
    above a ``since`` cursor. Every identifier comes from the contract; every
    caller value is a bound parameter.
    """
    relation = quote_identifier(cfg.table)
    retain = cfg.limit * RETAINED_WINDOW_FACTOR
    where: list[str] = []
    params: list[Any] = []

    if cfg.tenant_column is not None:
        if tenant_id is None:
            raise ProjectionReadError(
                "tenant_context_unresolved",
                f"exposure {cfg.topic!r} is scoped by {cfg.tenant_column!r} "
                "and the read carried no tenant",
                status_code=422,
            )
        params.append(tenant_id)
        where.append(f"CAST({quote_identifier(cfg.tenant_column)} AS TEXT) = ?")

    if correlation_id is not None:
        params.append(correlation_id)
        where.append(f"CAST({quote_identifier('correlation_id')} AS TEXT) = ?")

    if since is not None:
        if cfg.cursor_column is None:
            raise ProjectionReadError(
                "unsupported_filter",
                f"exposure {cfg.topic!r} declares no cursor_column",
                status_code=422,
            )
        params.append(since)
        where.append(f"{quote_identifier(cfg.cursor_column)} > ?")
        window_order = f"{quote_identifier(cfg.cursor_column)} ASC"
    else:
        recency = recency_column(cfg)
        window_order = (
            f"{quote_identifier(recency)} DESC"
            if recency is not None
            else order_clause(order_spec)
        )

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    inner_order = f" ORDER BY {window_order}" if window_order else ""
    inner = (
        f"SELECT {select_list(cfg)} FROM {relation}{where_sql}{inner_order} "
        f"LIMIT {retain}"
    )
    outer_order = order_clause(order_spec)
    sql = f"SELECT * FROM ({inner}) AS served_window" + (
        f" ORDER BY {outer_order}" if outer_order else ""
    )
    return WindowQuery(sql=sql, params=tuple(params))


def _driver_refusal(
    cfg: ProjectionTableConfig, exc: sqlite3.Error
) -> ProjectionReadError:
    """The named refusal for a driver error; the detail never carries its text."""
    message = str(exc).lower()
    if "no such table" in message:
        return ProjectionReadError(
            "projection_table_missing",
            f"the local store has no table {cfg.table!r}",
        )
    if "no such column" in message:
        return ProjectionReadError(
            "projection_column_missing",
            f"{cfg.table!r} lacks a column the exposure's contract declares",
        )
    return ProjectionReadError(
        "projection_database_unavailable", f"reading {cfg.table!r} failed"
    )


def _require_relation(
    conn: sqlite3.Connection,
    cfg: ProjectionTableConfig,
    order_spec: tuple[tuple[str, str, str | None], ...],
) -> None:
    """Refuse by name unless the table and every column the read names exist.

    SQLite reads a double-quoted identifier that names no column as a string
    literal, so a missing column would not fail the query: every row would
    carry the column's own name as its value. The catalogue is checked first.
    """
    present = {
        str(row[1])
        for row in conn.execute(
            "SELECT * FROM pragma_table_info(?)", (cfg.table.strip('"'),)
        )
    }
    if not present:
        raise ProjectionReadError(
            "projection_table_missing",
            f"the local store has no table {cfg.table!r}",
        )
    named = {
        *(() if cfg.columns == ("*",) else cfg.columns),
        *(column for column, _direction, _nulls in order_spec),
        *(
            column
            for column in (
                cfg.tenant_column,
                cfg.cursor_column,
                cfg.freshness_column,
            )
            if column is not None
        ),
    }
    if any(column.strip('"') not in present for column in named):
        raise ProjectionReadError(
            "projection_column_missing",
            f"{cfg.table!r} lacks a column the exposure's contract declares",
        )


class SqliteTableRowSource:
    """Read projection exposures from the local SQLite store.

    Each read opens and closes its own connection, so nothing is held open
    between reads and there is nothing to close.
    """

    backing = "sqlite_table"

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        if not self._db_path.is_file():
            raise ProjectionReadError(
                "projection_database_unavailable",
                "the local projection store named by the runtime binding does not exist",
            )
        try:
            conn = sqlite3.connect(
                f"{self._db_path.resolve().as_uri()}?mode=ro", uri=True
            )  # no-contract-check: read-only projection boundary; local SQLite projection reader (OMN-20329)
        except sqlite3.Error as exc:
            raise ProjectionReadError(
                "projection_database_unavailable",
                "could not open the local projection store",
            ) from exc
        conn.row_factory = sqlite3.Row
        return conn

    def _fetch(
        self,
        cfg: ProjectionTableConfig,
        order_spec: tuple[tuple[str, str, str | None], ...],
        query: WindowQuery,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            _require_relation(conn, cfg, order_spec)
            records = conn.execute(query.sql, query.params).fetchall()
        except sqlite3.Error as exc:
            raise _driver_refusal(cfg, exc) from exc
        finally:
            conn.close()
        return [serialise_row(cfg, dict(record)) for record in records]

    async def rows(
        self,
        cfg: ProjectionTableConfig,
        *,
        order_spec: tuple[tuple[str, str, str | None], ...],
        tenant_id: str | None,
        since: str | None = None,
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """The exposure's served window, ordered by ``order_spec``."""
        query = build_sqlite_window_query(
            cfg,
            order_spec=order_spec,
            tenant_id=tenant_id,
            since=since,
            correlation_id=correlation_id,
        )
        return await asyncio.to_thread(self._fetch, cfg, order_spec, query)

    async def latest_event_at(
        self,
        cfg: ProjectionTableConfig,
        *,
        tenant_id: str | None,
        window_rows: list[dict[str, Any]] | None = None,
    ) -> datetime | None:
        """Newest ``freshness_column`` value across the newest-rows window."""
        if cfg.freshness_column is None:
            return None
        rows = window_rows
        if rows is None:
            rows = await self.rows(cfg, order_spec=(), tenant_id=tenant_id)
        column = cfg.freshness_column.strip('"')
        values = [
            parsed
            for parsed in (_parse_timestamp(row.get(column)) for row in rows)
            if parsed is not None
        ]
        return max(values, default=None)

    def unavailable(self, topic: str) -> tuple[str, str] | None:
        """A table read has no warm-up: nothing is unavailable before a read."""
        return None

    def staleness(self, topic: str, latest_ts: str | None) -> dict[str, object]:
        """The staleness block for a table read: the store is the writer's own state."""
        return {
            "stale": False,
            "source": "sqlite_table",
            "lag_records": None,
            "applied_offset": None,
            "end_offset": None,
            "partitions_measured": 0,
            "last_applied_event_at": latest_ts,
            "dropped_since_apply": 0,
            "dropped_total": 0,
            "last_dropped_event_at": None,
        }

    async def page_view(
        self,
        topic_map: dict[str, ProjectionTableConfig],
        *,
        tenant_id: str | None,
    ) -> TablePageView:
        """Read every served exposure once for the status page."""
        view = TablePageView()
        for topic, cfg in topic_map.items():
            if not cfg.bus_backed:
                continue
            scoped = tenant_id if cfg.tenant_column is not None else None
            try:
                rows = await self.rows(
                    cfg, order_spec=cfg.order_by_spec, tenant_id=scoped
                )
            except ProjectionReadError as exc:
                view.failures[topic] = (exc.code, exc.detail)
                continue
            view.rows[topic] = rows
            view.tenants[topic] = scoped
            view.latest[topic] = await self.latest_event_at(
                cfg, tenant_id=scoped, window_rows=rows
            )
        return view

    def _probe(self, cfg: ProjectionTableConfig) -> None:
        conn = self._connect()
        try:
            _require_relation(conn, cfg, cfg.order_by_spec)
        except sqlite3.Error as exc:
            raise _driver_refusal(cfg, exc) from exc
        finally:
            conn.close()

    async def readiness(
        self, topic_map: dict[str, ProjectionTableConfig]
    ) -> tuple[bool, dict[str, object]]:
        """Ready when the store exists; a drifted exposure is named, not fatal."""
        served = sorted(t for t, cfg in topic_map.items() if cfg.bus_backed)
        failures: dict[str, dict[str, str]] = {}
        for topic in served:
            try:
                await asyncio.to_thread(self._probe, topic_map[topic])
            except ProjectionReadError as exc:
                failures[topic] = {"error": exc.code, "detail": exc.detail}
        process_failures = [
            failure
            for failure in failures.values()
            if failure["error"] == "projection_database_unavailable"
        ]
        ready = bool(served) and not process_failures
        return ready, {
            "status": "ready" if ready else "not_ready",
            "backing": self.backing,
            "served_topics": {topic: topic not in failures for topic in served},
            "failures": failures,
        }

    def health(self, topic_map: dict[str, ProjectionTableConfig]) -> dict[str, object]:
        return {
            "status": "ok",
            "backing": self.backing,
            "served_topics": sorted(t for t, c in topic_map.items() if c.bus_backed),
        }


__all__ = [
    "SqliteTableRowSource",
    "build_sqlite_window_query",
]
