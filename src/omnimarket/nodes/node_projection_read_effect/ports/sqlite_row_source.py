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
  the WHERE clause alone. A slug is resolved to its registry UUID first, from
  the store's own ``tenant_registry_mirror`` (OMN-19972).
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
from uuid import UUID

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import (
    RETAINED_WINDOW_FACTOR,
    TENANT_REGISTRY_UNREADABLE,
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
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
    TenantRegistryResolutionError,
    sync_registry_tenant_uuid,
)


def build_sqlite_window_query(
    cfg: ProjectionTableConfig,
    *,
    order_spec: tuple[tuple[str, str, str | None], ...],
    tenant_id: str | None,
    since: str | None = None,
    correlation_id: str | None = None,
    selection: str = "newest",
) -> WindowQuery:
    """The SQLite SQL for one exposure's served window.

    The window is the one :func:`table_reader.build_window_query` reads: the
    ``limit * 4`` rows ``selection`` names (``newest`` by the recency column,
    ``walk`` the oldest by it, ``ranked`` the top of the declared order), or
    the next ``limit * 4`` above a ``since`` cursor. Every identifier comes
    from the contract; every caller value is a bound parameter.
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
    elif selection == "ranked":
        window_order = order_clause(order_spec)
    else:
        recency = recency_column(cfg)
        direction = "ASC" if selection == "walk" else "DESC"
        window_order = (
            f"{quote_identifier(recency)} {direction}"
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


class _ReadOnlyRegistryQuery:
    """The one ``query`` the sync registry lookup issues, on a read-only connection.

    :func:`sync_registry_tenant_uuid` is the writer's own lookup; it reads
    through ``db.query(table, {column: value})`` and treats ``[]`` as "no row".
    ``SqliteDatabaseAdapter`` answers ``[]`` for a table the store never created,
    so this does the same, and every other driver error propagates.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def query(self, table: str, predicate: dict[str, str]) -> list[dict[str, Any]]:
        ((column, value),) = predicate.items()
        try:
            rows = self._conn.execute(
                f"SELECT tenant_uuid FROM {quote_identifier(table)} "
                f"WHERE {quote_identifier(column)} = ?",
                (value,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise
        return [dict(row) for row in rows]


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
            conn = sqlite3.connect(  # no-contract-check: read-only projection boundary; local SQLite projection reader (OMN-20329)
                f"{self._db_path.resolve().as_uri()}?mode=ro", uri=True
            )
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

    def _registry_lookup(self, tenant_slug: str) -> UUID | None:
        conn = self._connect()
        try:
            return sync_registry_tenant_uuid(_ReadOnlyRegistryQuery(conn), tenant_slug)
        except TenantRegistryResolutionError as exc:
            raise ProjectionReadError(
                TENANT_REGISTRY_UNREADABLE,
                f"{TENANT_REGISTRY_MIRROR_TABLE} holds a value for the requested "
                "tenant that is not a UUID",
            ) from exc
        except sqlite3.Error as exc:
            raise ProjectionReadError(
                TENANT_REGISTRY_UNREADABLE,
                f"reading {TENANT_REGISTRY_MIRROR_TABLE} from the local store failed",
            ) from exc
        finally:
            conn.close()

    async def registry_tenant_uuid(
        self, cfg: ProjectionTableConfig, tenant_slug: str
    ) -> UUID | None:
        """The registry UUID for ``tenant_slug`` in this store's own mirror (OMN-19972).

        ``onex local init`` records the install's identity in the store's
        ``tenant_registry_mirror``, and the local writers resolve a slug
        against it, so a slug is read back through the same relation.
        """
        return await asyncio.to_thread(self._registry_lookup, tenant_slug)

    async def rows(
        self,
        cfg: ProjectionTableConfig,
        *,
        order_spec: tuple[tuple[str, str, str | None], ...],
        tenant_id: str | None,
        since: str | None = None,
        correlation_id: str | None = None,
        selection: str = "newest",
    ) -> list[dict[str, Any]]:
        """The exposure's served window, ordered by ``order_spec``."""
        query = build_sqlite_window_query(
            cfg,
            order_spec=order_spec,
            tenant_id=tenant_id,
            since=since,
            correlation_id=correlation_id,
            selection=selection,
        )
        return await asyncio.to_thread(self._fetch, cfg, order_spec, query)

    def _smallest_cursor(
        self, cfg: ProjectionTableConfig, cursor_column: str, tenant_id: str | None
    ) -> object:
        where = ""
        params: tuple[Any, ...] = ()
        if cfg.tenant_column is not None and tenant_id is not None:
            where = f" WHERE CAST({quote_identifier(cfg.tenant_column)} AS TEXT) = ?"
            params = (tenant_id,)
        conn = self._connect()
        try:
            _require_relation(conn, cfg, ())
            row = conn.execute(
                f"SELECT min({quote_identifier(cursor_column)}) "
                f"FROM {quote_identifier(cfg.table)}{where}",
                params,
            ).fetchone()
        except sqlite3.Error as exc:
            raise _driver_refusal(cfg, exc) from exc
        finally:
            conn.close()
        return None if row is None else row[0]

    async def walk_origin(
        self, cfg: ProjectionTableConfig, *, tenant_id: str | None
    ) -> str | None:
        """The ``since`` value that starts an ascending walk at the first row.

        ``since`` is a strict ``>``, so the origin is one below the smallest
        cursor. Only an integer cursor has one; any other cursor type has no
        value a caller could be handed, and the answer is ``None``.
        """
        if cfg.cursor_column is None:
            return None
        smallest = await asyncio.to_thread(
            self._smallest_cursor, cfg, cfg.cursor_column, tenant_id
        )
        if isinstance(smallest, int) and not isinstance(smallest, bool):
            return str(smallest - 1)
        return None

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
