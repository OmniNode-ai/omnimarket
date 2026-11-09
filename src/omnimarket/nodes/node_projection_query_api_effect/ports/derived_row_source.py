# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serve an exposure that is a view without re-running the view on every read.

The overview exposures (the delegation summary, token usage, model routing,
quality gate, daily, savings and the cost savings overview) are views over
``delegation_events``. One evaluation of such a view scans that table (about
100k rows on the dev lane) several times and costs 0.13 to 2.4 s, and the
per-key skip scan :class:`TableRowSource` reads a mutable-grain exposure with
names the relation three times (an anchor, a recursive step that finds nothing,
and the per-key lookup), so a single HTTP read evaluated the whole aggregate
three times: 1.6 to 17 s measured on the dev lane at 2026-11-09 13:52Z, against
the dashboard's 5 s read timeout (OMN-23347).

:class:`DerivedReadRowSource` is a :class:`TableRowSource` that reads such a
relation once, with one ``DISTINCT ON`` pass, and holds the served window in a
:class:`DerivedWindowCache` for a bounded age. Every other read (a real table, a
``since`` walk, a ``correlation_id`` filter, a ranked window) is answered by the
base class unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import (
    RETAINED_WINDOW_FACTOR,
    ProjectionReadError,
    TableRowSource,
    _read_failure,
    order_clause,
    physical_key_columns,
    qualified_relation,
    quote_identifier,
    recency_column,
    select_list,
    serialise_row,
)
from omnimarket.projection.tenant_isolation import TENANT_GUC

log = logging.getLogger(__name__)

#: ``pg_class.relkind`` of a relation computed on every read: a view and a
#: materialized view.
DERIVED_RELKINDS: frozenset[str] = frozenset({"v", "m"})

#: How long the served window of a derived relation is answered from memory
#: before a refresh starts. A reader never waits for an evaluation once one
#: exists: it gets the held rows, and the first reader past this age starts the
#: refresh.
DEFAULT_DERIVED_READ_TTL_SECONDS = 30.0

#: The age past which a held window is no longer served while a refresh runs; a
#: read then waits for the refresh. Bounds how stale a refresh that keeps
#: failing can leave the answer.
DEFAULT_DERIVED_READ_MAX_STALE_SECONDS = 300.0

LoadWindow = Callable[[], Awaitable[list[dict[str, Any]]]]


class DerivedWindowCache:
    """Hold the served window of each derived relation read, bounded in age.

    The held window is served while it is younger than ``ttl_seconds``; past
    that the first reader starts one background refresh and every reader, that
    one included, is still answered from the held window until it is older than
    ``max_stale_seconds``. A read that finds nothing held, or only a window
    past the stale bound, waits on the one in-flight evaluation shared by all
    readers of that key, so concurrent readers never start a second pass. A
    failed refresh keeps the held window and the next read tries again. A
    ``ttl_seconds`` of zero or less holds nothing.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_stale_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_stale = max_stale_seconds
        self._clock = clock
        self._held: dict[Any, tuple[float, list[dict[str, Any]]]] = {}
        self._inflight: dict[Any, asyncio.Task[list[dict[str, Any]]]] = {}
        self._refreshes: set[asyncio.Task[list[dict[str, Any]]]] = set()

    async def read(self, key: Any, load: LoadWindow) -> list[dict[str, Any]]:
        if self._ttl <= 0:
            return await load()
        held = self._held.get(key)
        if held is not None:
            age = self._clock() - held[0]
            if age < self._ttl:
                return [dict(row) for row in held[1]]
            if age < self._max_stale:
                self._start(key, load, background=True)
                return [dict(row) for row in held[1]]
        rows = await asyncio.shield(self._start(key, load, background=False))
        return [dict(row) for row in rows]

    def _start(
        self, key: Any, load: LoadWindow, *, background: bool
    ) -> asyncio.Task[list[dict[str, Any]]]:
        task = self._inflight.get(key)
        if task is not None:
            return task

        async def fill() -> list[dict[str, Any]]:
            try:
                rows = await load()
            finally:
                self._inflight.pop(key, None)
            self._held[key] = (self._clock(), rows)
            return rows

        task = asyncio.ensure_future(fill())
        self._inflight[key] = task
        if background:
            self._refreshes.add(task)
            task.add_done_callback(self._refreshed)
        return task

    def _refreshed(self, task: asyncio.Task[list[dict[str, Any]]]) -> None:
        self._refreshes.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning(
                "refresh of a derived projection window failed; keeping the held "
                "window: %r",
                task.exception(),
            )


class DerivedReadRowSource(TableRowSource):
    """A :class:`TableRowSource` that reads a view once and holds the answer."""

    def __init__(
        self,
        environ: dict[str, str] | None = None,
        *,
        derived_read_ttl_seconds: float = DEFAULT_DERIVED_READ_TTL_SECONDS,
        derived_read_max_stale_seconds: float = DEFAULT_DERIVED_READ_MAX_STALE_SECONDS,
    ) -> None:
        super().__init__(environ)
        self._derived_windows = DerivedWindowCache(
            ttl_seconds=derived_read_ttl_seconds,
            max_stale_seconds=derived_read_max_stale_seconds,
        )
        self._relation_kinds: dict[str, str] = {}

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
        """The exposure's served window; a view's is one held pass over it."""
        keyed = bool(cfg.key_columns) and recency_column(cfg) is not None
        scoped = cfg.tenant_column is None or tenant_id is not None
        plain = since is None and correlation_id is None and selection == "newest"
        if not (keyed and scoped and plain) or not await self._is_derived(cfg):
            return await super().rows(
                cfg,
                order_spec=order_spec,
                tenant_id=tenant_id,
                since=since,
                correlation_id=correlation_id,
                selection=selection,
            )
        return await self._derived_windows.read(
            (qualified_relation(cfg), tenant_id, order_spec),
            lambda: self._read_derived(cfg, order_spec=order_spec, tenant_id=tenant_id),
        )

    async def _is_derived(self, cfg: ProjectionTableConfig) -> bool:
        """Whether the exposure's relation is a view, read once from the catalogue.

        An unanswered catalogue (the relation is missing, or the database cannot
        be reached) is not derived and caches nothing: the base read then
        refuses by name.
        """
        relation = qualified_relation(cfg)
        kind = self._relation_kinds.get(relation)
        if kind is None:
            pool = await self._pool(cfg)
            try:
                async with pool.acquire() as connection:
                    found = await connection.fetchval(
                        "SELECT relkind::text FROM pg_class "
                        "WHERE oid = to_regclass($1)",
                        relation,
                    )
            except (OSError, TimeoutError, asyncpg.PostgresError):
                return False
            if not isinstance(found, str) or len(found) != 1:
                return False
            kind = self._relation_kinds[relation] = found
        return kind in DERIVED_RELKINDS

    async def _read_derived(
        self,
        cfg: ProjectionTableConfig,
        *,
        order_spec: tuple[tuple[str, str, str | None], ...],
        tenant_id: str | None,
    ) -> list[dict[str, Any]]:
        """The newest row of each key, in one pass over the view."""
        pool = await self._pool(cfg)
        relation = qualified_relation(cfg)
        recency = recency_column(cfg)
        assert recency is not None
        try:
            async with (
                pool.acquire() as connection,
                connection.transaction(readonly=True),
            ):
                params: list[Any] = []
                where = ""
                if tenant_id is not None:
                    await connection.execute(
                        "SELECT set_config($1, $2, true)", TENANT_GUC, tenant_id
                    )
                if cfg.tenant_column is not None:
                    params.append(tenant_id)
                    where = f" WHERE {quote_identifier(cfg.tenant_column)}::text = $1"
                columns = await self._columns_of(connection, cfg)
                keys = ", ".join(
                    quote_identifier(column)
                    for column in physical_key_columns(cfg, columns)
                )
                distinct = f"DISTINCT ON ({keys}) " if keys else ""
                lead = f"{keys}, " if keys else ""
                newest = quote_identifier(cfg.latest_by or recency)
                outer = order_clause(order_spec)
                sql = (
                    "SELECT * FROM ("
                    f"SELECT {select_list(cfg)} FROM ("
                    f"SELECT {distinct}{select_list(cfg)} "
                    f"FROM {relation} AS t{where} ORDER BY {lead}t.{newest} DESC"
                    f") AS latest ORDER BY {quote_identifier(recency)} DESC NULLS LAST "
                    f"LIMIT {cfg.limit * RETAINED_WINDOW_FACTOR}"
                    f") AS served_window" + (f" ORDER BY {outer}" if outer else "")
                )
                records = await connection.fetch(sql, *params)
        except ProjectionReadError:
            raise
        except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
            raise _read_failure(cfg, relation, exc) from exc
        return [serialise_row(cfg, record) for record in records]
