# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serve projection reads from the materialized projection tables (OMN-20152).

Operator, 2026-09-30: "One delegation row is bullshit because everything should
be gathered from projections all that information is in the fucking database."

The projection writers (``node_projection_*``) already persist every row they
fold into Postgres. The projection API used to answer from an in-memory
``SnapshotCache`` rebuilt from Kafka instead, so every restart served only the
rows that arrived after it (``delegation.decisions.v1`` answered ``row_count=1``
over a table holding 4275), and the process could not answer at all until the
broker's partition metadata resolved. This module is the read path that
replaces it: each request reads the writer's own table, so a restart loses
nothing and no read waits on Kafka.

What a read returns is the same window the cache served, taken from the
durable table instead of from memory:

* the cache retained the newest ``limit * 4`` rows of an exposure (its
  eviction cap), so a request without ``since`` reads the newest
  ``limit * 4`` rows by the exposure's recency column (``cursor_column``,
  else ``freshness_column``) and the route pages and orders them exactly as
  before;
* a ``since`` walk reads the next ``limit * 4`` rows above the cursor from the
  whole table, where the cache could only walk what it happened to retain.

Tenant scoping (OMN-15797 AC2) is enforced twice, because the tables differ:
the row's own ``tenant_column`` is compared in the WHERE clause, and
``app.tenant_id`` is set for the transaction so an RLS policy on the relation
agrees with it. Rows are serialised the way the writers serialised them onto
the snapshot topics (timestamps as ISO strings, UUIDs and decimals as strings,
``json_columns`` decoded), so a client sees the same row shape it always did.

Every failure is a typed :class:`ProjectionReadError` naming the relation, the
route turns it into an explicit ``503`` (or ``422`` for a malformed ``since``),
never an empty ``200``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import asyncpg

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.tenant_isolation import TENANT_GUC

log = logging.getLogger(__name__)

#: The cache's eviction cap was ``limit * 4`` rows per exposure. A read serves
#: the same window size, so every page, cursor and truncation flag a client
#: saw from the cache has the same meaning here.
RETAINED_WINDOW_FACTOR = 4

#: The DSN each relation schema is read through. ``omninode_internal`` holds
#: the platform-internal read models and is readable by the runtime's role
#: (the same variable ``node_consumer_flow_stall_alert_effect`` declares as its
#: ``dsn_env`` for the same tables); every other schema is the dashboard
#: database, read through the dashboard role.
_RELATION_SCHEMA_DSN_ENV: dict[str, str] = {
    "omninode_internal": "OMNINODE_INTERNAL_DB_URL",
}
DEFAULT_DSN_ENV = "OMNIDASH_ANALYTICS_DB_URL"

_POOL_MAX_SIZE = 4
_CONNECT_TIMEOUT_SECONDS = 5.0
_COMMAND_TIMEOUT_SECONDS = 15.0


#: Read failures that mean the process cannot serve reads at all, as opposed to
#: one exposure's relation being wrong.
_PROCESS_LEVEL_READ_ERRORS: frozenset[str] = frozenset(
    {"projection_database_unbound", "projection_database_unavailable"}
)


class ProjectionReadError(Exception):
    """A read that could not be answered, with the code the route returns.

    ``code`` is a fixed identifier, ``detail`` is written for an operator and
    never carries driver exception text (this surface is reachable by an
    external caller).
    """

    def __init__(self, code: str, detail: str, *, status_code: int = 503) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status_code = status_code


def dsn_env_for(cfg: ProjectionTableConfig) -> str:
    """The environment variable holding the DSN this exposure is read through."""
    schema = cfg.relation_schema or ""
    return _RELATION_SCHEMA_DSN_ENV.get(schema, DEFAULT_DSN_ENV)


def quote_identifier(name: str) -> str:
    """Double-quote one SQL identifier taken from a contract."""
    bare = name.strip().strip('"')
    return '"' + bare.replace('"', '""') + '"'


def qualified_relation(cfg: ProjectionTableConfig) -> str:
    """``"schema"."table"`` for the relation the writer materializes."""
    if cfg.relation_schema is None:
        raise ProjectionReadError(
            "projection_relation_unresolved",
            f"exposure {cfg.topic!r} declares table {cfg.table!r} but no "
            "physical schema resolved for it from projection_api.schema or the "
            "writer's db_io",
        )
    return f"{quote_identifier(cfg.relation_schema)}.{quote_identifier(cfg.table)}"


def select_list(cfg: ProjectionTableConfig) -> str:
    if cfg.columns == ("*",):
        return "*"
    return ", ".join(quote_identifier(column) for column in cfg.columns)


def order_clause(spec: Iterable[tuple[str, str, str | None]]) -> str:
    """Render an order spec, keeping the cache's NULLS placement.

    The cache put nulls LAST whenever the contract omitted a NULLS clause,
    whatever the direction. Postgres defaults DESC to NULLS FIRST, so the
    clause is always stated.
    """
    terms = [
        f"{quote_identifier(column)} {direction} NULLS {nulls or 'LAST'}"
        for column, direction, nulls in spec
    ]
    return ", ".join(terms)


def recency_column(cfg: ProjectionTableConfig) -> str | None:
    """The column whose newest values define the served window."""
    return cfg.cursor_column or cfg.freshness_column


def json_value(value: Any, *, decode_json: bool = False) -> Any:
    """Serialise one column value the way the snapshot writers did."""
    if isinstance(value, datetime) or hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, UUID | Decimal):
        return str(value)
    if decode_json and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def serialise_row(cfg: ProjectionTableConfig, record: Any) -> dict[str, Any]:
    return {
        key: json_value(value, decode_json=key in cfg.json_columns)
        for key, value in dict(record).items()
    }


@dataclass(frozen=True)
class WindowQuery:
    """One read of an exposure's window, as SQL text and its parameters."""

    sql: str
    params: tuple[Any, ...]


def build_window_query(
    cfg: ProjectionTableConfig,
    *,
    order_spec: tuple[tuple[str, str, str | None], ...],
    tenant_id: str | None,
    since: str | None = None,
    since_type: str | None = None,
    correlation_id: str | None = None,
) -> WindowQuery:
    """The SQL for one exposure's served window.

    Every identifier comes from the contract; every caller value is a bound
    parameter. ``since_type`` is the cursor column's catalogue type, read from
    ``pg_attribute`` (never from the caller), so the bound text compares with
    the column's own ordering rather than as a string.
    """
    relation = qualified_relation(cfg)
    columns = select_list(cfg)
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
        where.append(f"{quote_identifier(cfg.tenant_column)}::text = ${len(params)}")

    if correlation_id is not None:
        params.append(correlation_id)
        where.append(f"{quote_identifier('correlation_id')}::text = ${len(params)}")

    if since is not None:
        if cfg.cursor_column is None or since_type is None:
            raise ProjectionReadError(
                "unsupported_filter",
                f"exposure {cfg.topic!r} declares no cursor_column",
                status_code=422,
            )
        params.append(since)
        where.append(
            f"{quote_identifier(cfg.cursor_column)} > "
            f"CAST(${len(params)}::text AS {since_type})"
        )
        window_order = f"{quote_identifier(cfg.cursor_column)} ASC"
    else:
        recency = recency_column(cfg)
        # No NULLS clause on the window's recency order: Postgres serves
        # ``DESC`` (implicitly NULLS FIRST) as a backward scan of the column's
        # ascending index, while ``DESC NULLS LAST`` forces a full sort -- on
        # the dev lane's 20M-row consumer_flow_windows that was a 15 s timeout
        # against a 5 ms index scan. Which rows fall in the window does not
        # depend on where nulls sort; their order on the page does, and the
        # outer ORDER BY keeps the cache's NULLS LAST for that.
        window_order = (
            f"{quote_identifier(recency)} DESC"
            if recency is not None
            else order_clause(order_spec)
        )

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    inner_order = f" ORDER BY {window_order}" if window_order else ""
    inner = f"SELECT {columns} FROM {relation}{where_sql}{inner_order} LIMIT {retain}"
    outer_order = order_clause(order_spec)
    sql = f"SELECT * FROM ({inner}) AS served_window" + (
        f" ORDER BY {outer_order}" if outer_order else ""
    )
    return WindowQuery(sql=sql, params=tuple(params))


class ProtocolProjectionPageView(Protocol):
    """The synchronous read surface the status page renders from."""

    def unavailable_reason(self, topic: str) -> tuple[str, str] | None: ...

    def get_rows(
        self,
        topic: str,
        *,
        limit: int | None = None,
        tenant_column: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def latest_event_at(self, topic: str) -> datetime | None: ...

    def row_count(self, topic: str) -> int: ...


@dataclass
class TablePageView:
    """A prefetched, synchronous view for the status page (``GET /``).

    The page renders from a fixed set of reads with the same interface the
    cache offered (``is_bootstrapped``/``get_rows``/``latest_event_at``/
    ``row_count``), so the route awaits every read first and the page code
    stays synchronous.
    """

    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    latest: dict[str, datetime | None] = field(default_factory=dict)
    tenants: dict[str, str | None] = field(default_factory=dict)
    failures: dict[str, tuple[str, str]] = field(default_factory=dict)

    def unavailable_reason(self, topic: str) -> tuple[str, str] | None:
        if topic in self.failures:
            return self.failures[topic]
        if topic not in self.rows:
            return ("projection_not_read", "the page did not read this exposure")
        return None

    def is_bootstrapped(self, topic: str) -> bool:
        return self.unavailable_reason(topic) is None

    def get_rows(
        self,
        topic: str,
        *,
        limit: int | None = None,
        tenant_column: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if tenant_column is not None and tenant_id != self.tenants.get(topic):
            raise ValueError(
                f"page view for {topic!r} was read as tenant "
                f"{self.tenants.get(topic)!r}, not {tenant_id!r}"
            )
        rows = self.rows.get(topic, [])
        return rows if limit is None else rows[:limit]

    def latest_event_at(self, topic: str) -> datetime | None:
        return self.latest.get(topic)

    def row_count(self, topic: str) -> int:
        return len(self.rows.get(topic, []))


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class ProtocolProjectionRowSource(Protocol):
    """What the projection API's routes read an exposure through."""

    backing: str

    def unavailable(self, topic: str) -> tuple[str, str] | None: ...

    async def rows(
        self,
        cfg: ProjectionTableConfig,
        *,
        order_spec: tuple[tuple[str, str, str | None], ...],
        tenant_id: str | None,
        since: str | None = None,
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def latest_event_at(
        self,
        cfg: ProjectionTableConfig,
        *,
        tenant_id: str | None,
        window_rows: list[dict[str, Any]] | None = None,
    ) -> datetime | None: ...

    def staleness(self, topic: str, latest_ts: str | None) -> dict[str, object]: ...

    async def page_view(
        self,
        topic_map: dict[str, ProjectionTableConfig],
        *,
        tenant_id: str | None,
    ) -> ProtocolProjectionPageView: ...

    async def readiness(
        self, topic_map: dict[str, ProjectionTableConfig]
    ) -> tuple[bool, dict[str, object]]: ...

    def health(
        self, topic_map: dict[str, ProjectionTableConfig]
    ) -> dict[str, object]: ...


class TableRowSource:
    """Read projection exposures from the tables their writers materialize.

    Construction connects to nothing: pools are opened on the first read of a
    DSN, so the HTTP server answers from its first second whatever the state
    of the database or the broker.
    """

    backing = "table"

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else dict(os.environ)
        self._pools: dict[str, asyncpg.Pool] = {}
        self._pool_lock = asyncio.Lock()
        self._column_types: dict[tuple[str, str], str] = {}

    async def close(self) -> None:
        pools, self._pools = self._pools, {}
        for pool in pools.values():
            await pool.close()

    async def _pool(self, cfg: ProjectionTableConfig) -> asyncpg.Pool:
        env = dsn_env_for(cfg)
        dsn = self._environ.get(env, "").strip()
        if not dsn:
            raise ProjectionReadError(
                "projection_database_unbound",
                f"exposure {cfg.topic!r} is read through {env}, which is not set "
                "for this process",
            )
        async with self._pool_lock:
            pool = self._pools.get(env)
            if pool is None:
                try:
                    pool = await asyncpg.create_pool(
                        dsn,
                        min_size=0,
                        max_size=_POOL_MAX_SIZE,
                        timeout=_CONNECT_TIMEOUT_SECONDS,
                        command_timeout=_COMMAND_TIMEOUT_SECONDS,
                    )
                except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
                    log.warning("projection database pool for %s failed: %r", env, exc)
                    raise ProjectionReadError(
                        "projection_database_unavailable",
                        f"could not open the database named by {env}",
                    ) from exc
                self._pools[env] = pool
        return pool

    async def _cursor_type(
        self, connection: asyncpg.Connection, cfg: ProjectionTableConfig
    ) -> str:
        assert cfg.cursor_column is not None
        key = (qualified_relation(cfg), cfg.cursor_column)
        cached = self._column_types.get(key)
        if cached is not None:
            return cached
        found = await connection.fetchval(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = to_regclass($1) AND attname = $2 AND NOT attisdropped",
            key[0],
            cfg.cursor_column.strip('"'),
        )
        if not isinstance(found, str):
            raise ProjectionReadError(
                "projection_column_missing",
                f"{key[0]} has no cursor column {cfg.cursor_column!r}",
            )
        self._column_types[key] = found
        return found

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
        pool = await self._pool(cfg)
        relation = qualified_relation(cfg)
        try:
            async with (
                pool.acquire() as connection,
                connection.transaction(readonly=True),
            ):
                if tenant_id is not None:
                    await connection.execute(
                        "SELECT set_config($1, $2, true)", TENANT_GUC, tenant_id
                    )
                since_type = (
                    await self._cursor_type(connection, cfg)
                    if since is not None and cfg.cursor_column is not None
                    else None
                )
                query = build_window_query(
                    cfg,
                    order_spec=order_spec,
                    tenant_id=tenant_id,
                    since=since,
                    since_type=since_type,
                    correlation_id=correlation_id,
                )
                records = await connection.fetch(query.sql, *query.params)
        except ProjectionReadError:
            raise
        except asyncpg.InsufficientPrivilegeError as exc:
            raise ProjectionReadError(
                "projection_table_unreadable",
                f"the role behind {dsn_env_for(cfg)} may not read {relation}",
            ) from exc
        except asyncpg.UndefinedTableError as exc:
            raise ProjectionReadError(
                "projection_table_missing", f"{relation} does not exist"
            ) from exc
        except asyncpg.UndefinedColumnError as exc:
            raise ProjectionReadError(
                "projection_column_missing",
                f"{relation} lacks a column the exposure's contract declares",
            ) from exc
        except (
            asyncpg.InvalidTextRepresentationError,
            asyncpg.InvalidDatetimeFormatError,
            asyncpg.DataError,
        ) as exc:
            raise ProjectionReadError(
                "invalid_since",
                f"'since' is not a value of {relation}'s cursor column",
                status_code=422,
            ) from exc
        except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
            log.warning("projection read of %s failed: %r", relation, exc)
            raise ProjectionReadError(
                "projection_database_unavailable", f"reading {relation} failed"
            ) from exc
        return [serialise_row(cfg, record) for record in records]

    async def latest_event_at(
        self,
        cfg: ProjectionTableConfig,
        *,
        tenant_id: str | None,
        window_rows: list[dict[str, Any]] | None = None,
    ) -> datetime | None:
        """Newest ``freshness_column`` value across the newest-rows window.

        ``window_rows`` is the unfiltered newest-rows window when the caller
        already read it; a ``since`` walk or a filtered read did not, so the
        window is read here.
        """
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
        """The staleness block, stated for a table read.

        The cache could fall behind its topic and serve rows it had stopped
        updating (OMN-18905). A table read has no such lag: it is the writer's
        own durable state at the moment of the request. How far the WRITER is
        behind shows in ``data_freshness``, computed from the newest row
        against the contract's declared cadence.
        """
        return {
            "stale": False,
            "source": "table",
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

        async def read_one(topic: str, cfg: ProjectionTableConfig) -> None:
            scoped = tenant_id if cfg.tenant_column is not None else None
            try:
                rows = await self.rows(
                    cfg, order_spec=cfg.order_by_spec, tenant_id=scoped
                )
            except ProjectionReadError as exc:
                view.failures[topic] = (exc.code, exc.detail)
                return
            view.rows[topic] = rows
            view.tenants[topic] = scoped
            view.latest[topic] = await self.latest_event_at(
                cfg, tenant_id=scoped, window_rows=rows
            )

        await asyncio.gather(
            *(
                read_one(topic, cfg)
                for topic, cfg in topic_map.items()
                if cfg.bus_backed
            )
        )
        return view

    async def readiness(
        self, topic_map: dict[str, ProjectionTableConfig]
    ) -> tuple[bool, dict[str, object]]:
        """Ready when the databases behind the served exposures answer.

        Not ready when a DSN is unbound or a database cannot be reached: then
        no read this process serves can succeed. An exposure whose own
        relation is missing, unreadable to the role, or lacks a declared
        column is named under ``failures`` but does not fail readiness: its
        own reads already refuse by name, and one drifted contract must not
        take every other panel dark (the OMN-18905 principle), which on a
        cluster that routes by ``/ready`` is what failing the whole process
        would do.
        """
        served = sorted(t for t, cfg in topic_map.items() if cfg.bus_backed)
        failures: dict[str, dict[str, str]] = {}

        async def probe(topic: str) -> None:
            cfg = topic_map[topic]
            try:
                pool = await self._pool(cfg)
                relation = qualified_relation(cfg)
                async with pool.acquire() as connection:
                    # The exposure's own column list, so a contract that
                    # declares a column the table lacks is not ready either.
                    await connection.execute(
                        f"SELECT {select_list(cfg)} FROM {relation} LIMIT 0"
                    )
            except ProjectionReadError as exc:
                failures[topic] = {"error": exc.code, "detail": exc.detail}
            except asyncpg.InsufficientPrivilegeError:
                failures[topic] = {
                    "error": "projection_table_unreadable",
                    "detail": f"the role behind {dsn_env_for(cfg)} may not read it",
                }
            except asyncpg.UndefinedTableError:
                failures[topic] = {
                    "error": "projection_table_missing",
                    "detail": f"{cfg.relation_schema}.{cfg.table} does not exist",
                }
            except asyncpg.UndefinedColumnError:
                failures[topic] = {
                    "error": "projection_column_missing",
                    "detail": (
                        f"{cfg.relation_schema}.{cfg.table} lacks a column the "
                        "exposure's contract declares"
                    ),
                }
            except (OSError, TimeoutError, asyncpg.PostgresError):
                failures[topic] = {
                    "error": "projection_database_unavailable",
                    "detail": f"probing {cfg.relation_schema}.{cfg.table} failed",
                }

        await asyncio.gather(*(probe(topic) for topic in served))
        process_failures = {
            topic: failure
            for topic, failure in failures.items()
            if failure["error"] in _PROCESS_LEVEL_READ_ERRORS
        }
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
