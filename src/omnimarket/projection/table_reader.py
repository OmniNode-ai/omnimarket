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
agrees with it. Both are given the tenant in the form the writer stored it: a
slug is resolved to its registry UUID before either is set (OMN-19972, see
:meth:`TableRowSource.registry_tenant_uuid`). Rows are serialised the way the
writers serialised them onto the snapshot topics (timestamps as ISO strings,
UUIDs and decimals as strings, ``json_columns`` decoded), so a client sees the
same row shape it always did.

Every failure is a typed :class:`ProjectionReadError` naming the relation, the
route turns it into an explicit ``503`` (or ``422`` for a malformed ``since``),
never an empty ``200``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import asyncpg

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.tenant_isolation import TENANT_GUC
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
    TenantRegistryResolutionError,
    async_registry_tenant_uuid,
)

log = logging.getLogger(__name__)

#: The refusal code for a tenant registry that could not be read. Distinct from
#: ``tenant_context_unresolved`` (a 422 the caller can fix by naming a
#: registered tenant): this is server state, so it is a 503.
TENANT_REGISTRY_UNREADABLE = "tenant_registry_unreadable"

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

#: How long a relation's unique-index catalogue is answered from memory. Not
#: for the life of the process: a unique index dropped, or left invalid, while
#: the process runs must stop the fast path, or a mutable key would serve
#: every revision of a key until the next restart (OMN-19971).
_UNIQUE_KEY_CATALOGUE_TTL_SECONDS = 60.0


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


@dataclass(frozen=True)
class UniqueKeyCatalogue:
    """What a relation's catalogue says about the uniqueness of its rows.

    ``unique_keys`` are the column sets of the unique indexes that enforce
    uniqueness on every row; ``not_null_columns`` are the relation's
    ``NOT NULL`` columns (read only when it has a unique index at all).
    """

    unique_keys: tuple[frozenset[str], ...] = ()
    not_null_columns: frozenset[str] = frozenset()


def _latest_per_key_sql(
    cfg: ProjectionTableConfig,
    relation: str,
    *,
    key_columns: tuple[str, ...],
    tenant_where: str,
    order_column: str,
) -> str:
    """The newest row of each key, reached by a skip scan over the key index.

    A mutable-grain exposure serves one row per key -- the latest -- and the
    snapshot cache it replaced held exactly that. A table can hold every
    window a key ever had, so a window cut by recency over the table serves a
    handful of busy keys many times and never reaches the quiet ones
    (OMN-20327: consumer-flow's 2000 newest rows named 597 of its 976
    consumer-group/topic pairs). The distinct keys are found one index probe
    each, and the newest row of each is one more, so the cost follows the key
    count, not the table's row count.

    ``key_columns`` are the declared key columns the relation carries (see
    :func:`physical_key_columns`).
    """
    keys = ", ".join(quote_identifier(column) for column in key_columns)
    tenant_and = f" AND {tenant_where}" if tenant_where else ""
    tenant_only = f" WHERE {tenant_where}" if tenant_where else ""
    after_key = (
        "("
        + ", ".join(f"t.{quote_identifier(column)}" for column in key_columns)
        + ") > ("
        + ", ".join(f"k.{quote_identifier(column)}" for column in key_columns)
        + ")"
    )
    same_key = " AND ".join(
        f"t.{quote_identifier(column)} = k.{quote_identifier(column)}"
        for column in key_columns
    )
    return (
        "WITH RECURSIVE k AS ("
        f"(SELECT {keys} FROM {relation}{tenant_only} ORDER BY {keys} LIMIT 1) "
        f"UNION ALL SELECT {', '.join(f'n.{quote_identifier(c)}' for c in key_columns)} "
        f"FROM k, LATERAL (SELECT {keys} FROM {relation} AS t "
        f"WHERE {after_key}{tenant_and} ORDER BY {keys} LIMIT 1) AS n) "
        f"SELECT l.* FROM k CROSS JOIN LATERAL (SELECT {select_list(cfg)} "
        f"FROM {relation} AS t WHERE {same_key}{tenant_and} "
        f"ORDER BY t.{quote_identifier(order_column)} DESC LIMIT 1) AS l"
    )


def _dated_first_sql(
    columns: str,
    relation: str,
    *,
    scope: list[str],
    recency: str,
    retain: int,
) -> str:
    """The newest ``retain`` rows of ``relation`` by ``recency``, NULLs last.

    The dated rows are one bounded read through the recency column's index
    (``IS NOT NULL`` is an index condition); the undated rows only fill what
    the dated rows leave, and their read is never run when that is nothing.
    ``scope`` is the window's WHERE list, applied to both reads with the same
    bound parameters.
    """
    column = quote_identifier(recency)
    where = "".join(f"{clause} AND " for clause in scope)
    return (
        f"WITH newest_dated AS (SELECT {columns} FROM {relation} "
        f"WHERE {where}{column} IS NOT NULL ORDER BY {column} DESC LIMIT {retain}) "
        "SELECT * FROM newest_dated UNION ALL "
        f"(SELECT {columns} FROM {relation} WHERE {where}{column} IS NULL "
        f"LIMIT {retain} - (SELECT count(*) FROM newest_dated))"
    )


def physical_key_columns(
    cfg: ProjectionTableConfig, relation_columns: frozenset[str] | None
) -> tuple[str, ...]:
    """The declared key columns the relation carries, in declared order.

    A key column can be part of the bus compaction key without being a column
    of the relation: the delegation and savings aggregates key on
    ``snapshot_grain``, a constant the republish query mints and the view
    deliberately lacks (OMN-19971). Such a column can never select or order a
    row, so the latest-row-per-key read keys on the rest. ``None`` means the
    relation's columns are unknown, and every declared key is kept.
    """
    if relation_columns is None:
        return cfg.key_columns
    return tuple(
        column for column in cfg.key_columns if column.strip('"') in relation_columns
    )


def key_is_unique_per_row(
    cfg: ProjectionTableConfig,
    key_columns: tuple[str, ...],
    unique_keys: tuple[frozenset[str], ...],
    not_null_columns: frozenset[str] | None = None,
) -> bool:
    """Whether every row of the relation is the only row of its key.

    Then the latest row of each key is every row, and the recursive per-key
    scan only walks the whole table to find that out: ``work.events``,
    ``session.replay`` and ``live-events`` key on an event or snapshot id, so
    the key count is the row count (OMN-19971). True when the contract
    declares the key grain immutable (one source event owns the key for its
    life), or when the relation has a unique index whose columns are all
    among ``key_columns`` -- an index on part of the key makes the whole key
    unique too, while one that reaches past it does not.

    The index route also needs every key column ``NOT NULL`` in the relation
    (``not_null_columns``). A unique index lets any number of rows hold a
    NULL, and the per-key scan never serves a row with a NULL key column (it
    matches no key by equality), so on a nullable key a window cut straight
    from the relation would serve rows the scan never did. ``None`` means the
    nullability is unknown, and the index route is not taken.
    """
    if not key_columns:
        return False
    if cfg.key_grain == "immutable":
        return True
    physical = frozenset(column.strip('"') for column in key_columns)
    if not_null_columns is None or not physical <= not_null_columns:
        return False
    return any(index and index <= physical for index in unique_keys)


def build_window_query(
    cfg: ProjectionTableConfig,
    *,
    order_spec: tuple[tuple[str, str, str | None], ...],
    tenant_id: str | None,
    since: str | None = None,
    since_type: str | None = None,
    correlation_id: str | None = None,
    selection: str = "newest",
    relation_columns: frozenset[str] | None = None,
    unique_keys: tuple[frozenset[str], ...] = (),
    not_null_columns: frozenset[str] | None = None,
) -> WindowQuery:
    """The SQL for one exposure's served window.

    Every identifier comes from the contract; every caller value is a bound
    parameter. ``since_type`` is the cursor column's catalogue type, read from
    ``pg_attribute`` (never from the caller), so the bound text compares with
    the column's own ordering rather than as a string.

    ``selection`` names which ``limit * 4`` rows of the exposure the window
    holds: ``newest`` (the status page and every exposure without a cursor),
    ``walk`` (the start of the ascending cursor walk, so a read without
    ``since`` is page one of a walk that reaches every key) or ``ranked`` (the
    top of the declared order over the whole set). A ``since`` read is always
    a walk. A walk needs a cursor to continue from: asked of an exposure that
    declares none, it would serve the oldest rows with no way past them
    (OMN-19971), so the newest window is built instead.

    ``relation_columns`` are the relation's live column names; a declared key
    column outside them is left out of the latest-row-per-key read.
    ``unique_keys`` are the column sets of the relation's unique indexes and
    ``not_null_columns`` its ``NOT NULL`` columns; when they make the key
    unique per row (see :func:`key_is_unique_per_row`) the window is cut
    straight from the relation.
    """
    relation = qualified_relation(cfg)
    columns = select_list(cfg)
    retain = cfg.limit * RETAINED_WINDOW_FACTOR
    params: list[Any] = []
    tenant_where = ""

    if cfg.tenant_column is not None:
        if tenant_id is None:
            raise ProjectionReadError(
                "tenant_context_unresolved",
                f"exposure {cfg.topic!r} is scoped by {cfg.tenant_column!r} "
                "and the read carried no tenant",
                status_code=422,
            )
        params.append(tenant_id)
        tenant_where = f"{quote_identifier(cfg.tenant_column)}::text = ${len(params)}"

    where: list[str] = []
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

    recency = recency_column(cfg)
    newest_by_recency = False
    if since is not None or (selection == "walk" and cfg.cursor_column is not None):
        window_order = (
            f"{quote_identifier(recency)} ASC"
            if recency is not None
            else order_clause(order_spec)
        )
    elif selection == "ranked" or recency is None:
        window_order = order_clause(order_spec)
    else:
        # The newest rows by the recency column, rows without a value last,
        # as the cache served them and the SQLite store still does. Postgres
        # reads ``DESC`` as NULLS FIRST, so ``ORDER BY <recency> DESC LIMIT n``
        # filled the window with undated rows before any dated one and a page
        # lost its dated rows. ``DESC NULLS LAST`` cannot be served from the
        # column's ascending index and sorts the whole relation -- on the dev
        # lane's 20M-row consumer_flow_windows that was a 15 s timeout against
        # a 5 ms index scan -- so a window cut straight from the relation is
        # read in two branches: the newest dated rows, one bounded backward
        # scan of that index, then undated rows only to fill what the dated
        # rows leave. When the dated rows fill the window the second branch's
        # LIMIT is 0 and Postgres never runs its scan. The latest row of each
        # key is sorted in memory whatever the clause, so that window states
        # ``NULLS LAST`` outright (OMN-19971).
        newest_by_recency = True
        window_order = f"{quote_identifier(recency)} DESC NULLS LAST"

    key_columns = physical_key_columns(cfg, relation_columns)
    latest_per_key = (
        bool(key_columns)
        and recency is not None
        and not key_is_unique_per_row(cfg, key_columns, unique_keys, not_null_columns)
    )
    if latest_per_key:
        assert recency is not None
        source = f"({_latest_per_key_sql(cfg, relation, key_columns=key_columns, tenant_where=tenant_where, order_column=cfg.latest_by or recency)}) AS latest"
    else:
        if tenant_where:
            where.insert(0, tenant_where)
        source = relation
    if newest_by_recency and not latest_per_key:
        assert recency is not None
        inner = _dated_first_sql(
            columns, relation, scope=where, recency=recency, retain=retain
        )
    else:
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        inner_order = f" ORDER BY {window_order}" if window_order else ""
        inner = f"SELECT {columns} FROM {source}{where_sql}{inner_order} LIMIT {retain}"
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


def _read_failure(
    cfg: ProjectionTableConfig, relation: str, exc: BaseException
) -> ProjectionReadError:
    """The named refusal for a driver error on a read of ``relation``.

    The detail never carries the driver's text: this surface is reachable by
    an external caller.
    """
    if isinstance(exc, asyncpg.InsufficientPrivilegeError):
        return ProjectionReadError(
            "projection_table_unreadable",
            f"the role behind {dsn_env_for(cfg)} may not read {relation}",
        )
    if isinstance(exc, asyncpg.UndefinedTableError):
        return ProjectionReadError(
            "projection_table_missing", f"{relation} does not exist"
        )
    if isinstance(exc, asyncpg.UndefinedColumnError):
        return ProjectionReadError(
            "projection_column_missing",
            f"{relation} lacks a column the exposure's contract declares",
        )
    if isinstance(
        exc,
        (
            asyncpg.InvalidTextRepresentationError,
            asyncpg.InvalidDatetimeFormatError,
            asyncpg.DataError,
        ),
    ):
        return ProjectionReadError(
            "invalid_since",
            f"'since' is not a value of {relation}'s cursor column",
            status_code=422,
        )
    log.warning("projection read of %s failed: %r", relation, exc)
    return ProjectionReadError(
        "projection_database_unavailable", f"reading {relation} failed"
    )


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

    async def registry_tenant_uuid(
        self, cfg: ProjectionTableConfig, tenant_slug: str
    ) -> UUID | None:
        """The UUID ``tenant_registry_mirror`` records for ``tenant_slug``.

        ``None`` when the mirror holds no row for it, or when the store has no
        such relation (the write path's reading of the same two facts).
        Raises :class:`ProjectionReadError` when the mirror cannot be read or
        holds a value that is not a UUID -- never ``None`` for those.
        """

    async def rows(
        self,
        cfg: ProjectionTableConfig,
        *,
        order_spec: tuple[tuple[str, str, str | None], ...],
        tenant_id: str | None,
        since: str | None = None,
        correlation_id: str | None = None,
        selection: str = "newest",
    ) -> list[dict[str, Any]]: ...

    async def walk_origin(
        self, cfg: ProjectionTableConfig, *, tenant_id: str | None
    ) -> str | None: ...

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
        self._relation_columns: dict[str, frozenset[str]] = {}
        self._unique_keys: dict[str, tuple[float, UniqueKeyCatalogue]] = {}
        self._clock: Callable[[], float] = time.monotonic

    @classmethod
    def for_database_url(cls, database_url: str) -> TableRowSource:
        """Read every exposure through one database (OMN-20159).

        The runtime-resident read node is bound to the runtime's own projection
        database, where the writers materialize every table, so every relation
        schema is read through that one binding rather than per-schema DSNs.
        """
        dsn_envs = (DEFAULT_DSN_ENV, *_RELATION_SCHEMA_DSN_ENV.values())
        return cls(environ=dict.fromkeys(dsn_envs, database_url))

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
                    # The projection API process owns this read pool the way it
                    # owned the snapshot-cache consumer (OMN-15800): a read-only
                    # projection boundary, DSN-injected, in a read-only
                    # transaction per read. The tag is the scanner's sanctioned
                    # per-line boundary, as in postgres_read_database.py.
                    pool = await asyncpg.create_pool(  # no-contract-check: read-only projection boundary; DSN-injected projection API reader (OMN-20152)
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

    async def _columns_of(
        self, connection: asyncpg.Connection, cfg: ProjectionTableConfig
    ) -> frozenset[str] | None:
        """The relation's live column names, read once; ``None`` if it has none.

        Read only for an exposure whose window keys on its key columns. A
        relation that does not resolve answers ``None``, so every declared key
        is kept and the read itself names the missing table.
        """
        relation = qualified_relation(cfg)
        cached = self._relation_columns.get(relation)
        if cached is not None:
            return cached
        found = await connection.fetchval(
            "SELECT array_agg(attname::text) FROM pg_attribute "
            "WHERE attrelid = to_regclass($1) AND attnum > 0 AND NOT attisdropped",
            relation,
        )
        if not isinstance(found, list) or not found:
            return None
        columns = frozenset(str(column) for column in found)
        self._relation_columns[relation] = columns
        return columns

    async def _unique_keys_of(
        self, connection: asyncpg.Connection, cfg: ProjectionTableConfig
    ) -> UniqueKeyCatalogue:
        """The relation's unique indexes and ``NOT NULL`` columns.

        Only an index that enforces uniqueness over plain columns for every
        row counts: valid (a failed ``CREATE UNIQUE INDEX CONCURRENTLY``
        leaves an invalid index that enforces nothing), not partial (its
        predicate exempts every other row), with no expression column, and
        over its key columns alone (``INCLUDE`` columns take no part in
        uniqueness). Each key column must also be compared the way the
        per-key scan compares it, under the column's own collation and its
        type's default operator class: an index declared with another
        collation or operator class forbids rows that are equal under ITS
        equality, and ``pg_get_indexdef`` prints such a key column as the
        bare column name. Nullability comes from
        ``information_schema.columns.is_nullable``, which also counts a
        domain's ``NOT NULL``; a column the role cannot see there is treated
        as nullable. Read only for a relation whose columns resolved, so a
        relation that does not exist yet caches nothing and the read itself
        names it missing.

        The answer is kept for ``_UNIQUE_KEY_CATALOGUE_TTL_SECONDS`` and then
        read again, so an index dropped while the process runs stops the fast
        path without a restart.
        """
        relation = qualified_relation(cfg)
        now = self._clock()
        cached = self._unique_keys.get(relation)
        if cached is not None and now - cached[0] < _UNIQUE_KEY_CATALOGUE_TTL_SECONDS:
            return cached[1]
        records = await connection.fetch(
            "SELECT ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true) "
            "FROM generate_series(1, i.indnkeyatts) AS k ORDER BY k) AS key_columns, "
            "i.indisvalid AS is_valid, i.indpred IS NOT NULL AS is_partial, "
            "(SELECT count(*) FROM generate_series(1, i.indnkeyatts) AS k "
            "JOIN pg_attribute AS a ON a.attrelid = i.indrelid "
            "AND a.attnum = i.indkey[k - 1] "
            "JOIN pg_opclass AS o ON o.oid = i.indclass[k - 1] "
            "WHERE a.attcollation = i.indcollation[k - 1] AND o.opcdefault) "
            "= i.indnkeyatts AS plain_equality, "
            "ARRAY(SELECT c.column_name::text FROM information_schema.columns AS c "
            "WHERE c.table_schema = n.nspname AND c.table_name = r.relname "
            "AND c.is_nullable = 'NO') AS not_null_columns "
            "FROM pg_index AS i JOIN pg_class AS r ON r.oid = i.indrelid "
            "JOIN pg_namespace AS n ON n.oid = r.relnamespace "
            "WHERE i.indrelid = to_regclass($1) "
            "AND i.indisunique AND i.indexprs IS NULL",
            relation,
        )
        found = UniqueKeyCatalogue(
            unique_keys=tuple(
                frozenset(str(column).strip('"') for column in record["key_columns"])
                for record in records
                if record["is_valid"]
                and not record["is_partial"]
                and record["plain_equality"]
            ),
            not_null_columns=frozenset(
                str(column)
                for record in records
                for column in record["not_null_columns"]
            ),
        )
        self._unique_keys[relation] = (now, found)
        return found

    async def registry_tenant_uuid(
        self, cfg: ProjectionTableConfig, tenant_slug: str
    ) -> UUID | None:
        """The registry UUID for ``tenant_slug``, read the way the writer reads it.

        OMN-19972. The writer stamps the UUID ``tenant_registry_mirror``
        records for a slug (:func:`async_registry_tenant_uuid`), so the reader
        asks the same relation the same question, through the database this
        exposure is read from. A relation the lane has not created reads as
        ``None`` there and here; any other failure is a named refusal, never a
        ``None`` that would let the caller fall back to the slug.
        """
        pool = await self._pool(cfg)
        try:
            # One SELECT and no transaction, as the writer issues it: a lane
            # without the relation answers 42P01, which the lookup reads as
            # "no row", and there is no aborted transaction left to close.
            async with pool.acquire() as connection:
                return await async_registry_tenant_uuid(connection, tenant_slug)
        except TenantRegistryResolutionError as exc:
            raise ProjectionReadError(
                TENANT_REGISTRY_UNREADABLE,
                f"{TENANT_REGISTRY_MIRROR_TABLE} holds a value for the requested "
                "tenant that is not a UUID",
            ) from exc
        except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
            log.warning(
                "tenant registry read through %s failed: %r", dsn_env_for(cfg), exc
            )
            raise ProjectionReadError(
                TENANT_REGISTRY_UNREADABLE,
                f"reading {TENANT_REGISTRY_MIRROR_TABLE} through "
                f"{dsn_env_for(cfg)} failed",
            ) from exc

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
                keyed = bool(cfg.key_columns) and recency_column(cfg) is not None
                relation_columns = (
                    await self._columns_of(connection, cfg) if keyed else None
                )
                catalogue = (
                    await self._unique_keys_of(connection, cfg)
                    if keyed
                    and relation_columns is not None
                    and cfg.key_grain != "immutable"
                    else UniqueKeyCatalogue()
                )
                query = build_window_query(
                    cfg,
                    order_spec=order_spec,
                    tenant_id=tenant_id,
                    since=since,
                    since_type=since_type,
                    correlation_id=correlation_id,
                    selection=selection,
                    relation_columns=relation_columns,
                    unique_keys=catalogue.unique_keys,
                    not_null_columns=catalogue.not_null_columns,
                )
                records = await connection.fetch(query.sql, *query.params)
        except ProjectionReadError:
            raise
        except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
            raise _read_failure(cfg, relation, exc) from exc
        return [serialise_row(cfg, record) for record in records]

    async def _newest_freshness(
        self, cfg: ProjectionTableConfig, *, tenant_id: str | None
    ) -> datetime | None:
        """The newest ``freshness_column`` value of the exposure, bounded.

        One read through an index the exposure's own window already relies
        on, scoped to the tenant like the window, instead of a second whole
        window (OMN-19971).

        An exposure without a cursor orders its window by the freshness
        column, so its newest value is one backward step of that column's
        index: ``ORDER BY <freshness> DESC LIMIT 1`` over the non-NULL values.

        A cursor exposure orders by its cursor, and nothing makes its
        freshness column the leading column of an index, so ordering the
        relation by it can be a full scan (``consumer-flow``'s table held
        42.6M rows on the lab and was not ordered by ``window_end`` within
        20 s). Its newest value is the largest among the newest ``limit * 4``
        rows by cursor, which the walk's own index serves.
        """
        assert cfg.freshness_column is not None
        pool = await self._pool(cfg)
        relation = qualified_relation(cfg)
        column = quote_identifier(cfg.freshness_column)
        params: list[Any] = []
        tenant_where = ""
        if cfg.tenant_column is not None:
            if tenant_id is None:
                raise ProjectionReadError(
                    "tenant_context_unresolved",
                    f"exposure {cfg.topic!r} is scoped by {cfg.tenant_column!r} "
                    "and the read carried no tenant",
                    status_code=422,
                )
            params.append(tenant_id)
            tenant_where = f"{quote_identifier(cfg.tenant_column)}::text = $1"
        if cfg.cursor_column is None:
            where = " AND ".join(
                clause for clause in (tenant_where, f"{column} IS NOT NULL") if clause
            )
            sql = (
                f"SELECT {column} FROM {relation} WHERE {where} "
                f"ORDER BY {column} DESC LIMIT 1"
            )
        else:
            scope = f" WHERE {tenant_where}" if tenant_where else ""
            retain = cfg.limit * RETAINED_WINDOW_FACTOR
            sql = (
                f"SELECT max({column}) FROM (SELECT {column} FROM {relation}{scope} "
                f"ORDER BY {quote_identifier(cfg.cursor_column)} DESC "
                f"LIMIT {retain}) AS newest"
            )
        try:
            async with (
                pool.acquire() as connection,
                connection.transaction(readonly=True),
            ):
                if tenant_id is not None:
                    await connection.execute(
                        "SELECT set_config($1, $2, true)", TENANT_GUC, tenant_id
                    )
                newest = await connection.fetchval(sql, *params)
        except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
            raise _read_failure(cfg, relation, exc) from exc
        return _parse_timestamp(newest)

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
        pool = await self._pool(cfg)
        relation = qualified_relation(cfg)
        where = ""
        params: list[Any] = []
        if cfg.tenant_column is not None and tenant_id is not None:
            params.append(tenant_id)
            where = f" WHERE {quote_identifier(cfg.tenant_column)}::text = $1"
        try:
            async with pool.acquire() as connection:
                smallest = await connection.fetchval(
                    f"SELECT min({quote_identifier(cfg.cursor_column)}) "
                    f"FROM {relation}{where}",
                    *params,
                )
        except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
            log.warning("walk origin read of %s failed: %r", relation, exc)
            raise ProjectionReadError(
                "projection_database_unavailable", f"reading {relation} failed"
            ) from exc
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
        """Newest ``freshness_column`` value of the exposure.

        ``window_rows`` is the unfiltered newest-rows window when the caller
        already read it, and the answer comes from those rows with nothing
        read. A walk, a ranked read or a filtered read holds other rows, so
        the newest value is read with one bounded query rather than a second
        window (OMN-19971: the second window doubled every read's cost).

        The newest window serves rows with no recency value last, so for an
        exposure without a cursor (whose recency column is its freshness
        column) it holds an undated row only when the scope has no further
        dated row: the newest value among its rows is the exposure's newest,
        and a window of only undated rows, or of none, means the scope holds
        no dated row. For an exposure with a cursor the newest value is the
        largest among its newest rows by cursor, which is that window.
        """
        if cfg.freshness_column is None:
            return None
        if window_rows is None:
            return await self._newest_freshness(cfg, tenant_id=tenant_id)
        column = cfg.freshness_column.strip('"')
        values = [
            parsed
            for parsed in (_parse_timestamp(row.get(column)) for row in window_rows)
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
            # Both reads inside the one ``try``: a refusal from either names
            # this exposure failed, and the page still renders every other.
            try:
                rows = await self.rows(
                    cfg, order_spec=cfg.order_by_spec, tenant_id=scoped
                )
                latest = await self.latest_event_at(
                    cfg, tenant_id=scoped, window_rows=rows
                )
            except ProjectionReadError as exc:
                view.failures[topic] = (exc.code, exc.detail)
                return
            view.rows[topic] = rows
            view.tenants[topic] = scoped
            view.latest[topic] = latest

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
