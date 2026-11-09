# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19986: the overview's aggregate reads answer well inside the dashboard's 5 s.

The local dashboard gives up on a read after 5 s. Seven overview exposures are
views over ``delegation_events`` (about 100k rows on the dev lane), and a read
of one evaluated the whole aggregate three times: the per-key skip scan names
the relation in its anchor, in a recursive step that finds nothing, and in the
per-key lookup. Measured on the dev lane at 2026-11-09 13:52Z the reads took
1.6 to 17 s.

The data below is recorded: ``EXPLAIN (ANALYZE)`` of one evaluation of each
view as the API's role reads it, on the dev lane's ``omnidash_analytics``
holding 102,584 ``delegation_events`` rows (2026-11-09). The fake connection
charges that recorded cost to a virtual clock once for each time the read's SQL
names the view, so a read's duration here is the recorded database time.

Failure modes these tests are written against:

* F1 a view is evaluated more than once per read;
* F2 a second read within the held window evaluates the view again;
* F3 a read past the held window waits for the refresh instead of answering;
* F4 concurrent readers of a cold view each start their own evaluation;
* F5 a failed refresh drops the held window or answers an error.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from omnimarket.projection import table_reader
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import (
    DEFAULT_DERIVED_READ_MAX_STALE_SECONDS,
    DEFAULT_DERIVED_READ_TTL_SECONDS,
    DEFAULT_DSN_ENV,
    DerivedWindowCache,
    TableRowSource,
)

_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
#: The dashboard's local read timeout (omnidash LOCAL_READ_TIMEOUT_MS = 5000).
_DASHBOARD_TIMEOUT_SECONDS = 5.0
#: The bound this fix holds a held read to.
_BUDGET_SECONDS = 2.0

#: view -> seconds of one evaluation, recorded on the dev lane 2026-11-09.
_RECORDED_EVALUATION_SECONDS: dict[str, float] = {
    "projection_delegation_summary": 0.431,
    "projection_delegation_token_usage": 0.342,
    "projection_delegation_model_routing": 2.404,
    "projection_delegation_quality_gate": 0.222,
    "projection_delegation_daily": 0.134,
    "projection_delegation_savings": 0.503,
    "projection_cost_savings_overview": 0.633,
}
#: view -> seconds the recursive per-key read took through the API before the
#: fix (best of three reads at 2026-11-09T13:52Z).
_RECORDED_BEFORE_SECONDS: dict[str, float] = {
    "projection_delegation_summary": 1.606,
    "projection_delegation_token_usage": 5.195,
    "projection_delegation_model_routing": 3.548,
    "projection_delegation_quality_gate": 1.581,
    "projection_delegation_daily": 0.870,
    "projection_delegation_savings": 8.395,
    "projection_cost_savings_overview": 4.409,
}
_COLUMNS = ("tenant_id", "total", "latest_projection_updated_at")


def _cfg(view: str) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=f"onex.snapshot.projection.{view}.v1",
        table=view,
        schema_name="public",
        relation_schema="public",
        columns=_COLUMNS,
        order_by="latest_projection_updated_at DESC",
        order_by_spec=parse_order_by_clauses(
            "latest_projection_updated_at DESC", _COLUMNS
        ),
        freshness_column="latest_projection_updated_at",
        limit=1,
        bus_backed=True,
        key_columns=("snapshot_grain", "tenant_id"),
        tenant_column="tenant_id",
    )


@dataclass
class _Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


class _Database:
    """A database whose views cost what the lab recorded, on a virtual clock."""

    def __init__(self, clock: _Clock, view: str) -> None:
        self.clock = clock
        self.view = view
        self.evaluations = 0
        self.fail = False
        self.total = 1

    def window(self) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": _TENANT,
                "total": self.total,
                "latest_projection_updated_at": None,
            }
        ]

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield _Connection(self)


class _Connection:
    def __init__(self, database: _Database) -> None:
        self._db = database

    @asynccontextmanager
    async def transaction(self, *, readonly: bool = False) -> Any:
        yield

    async def execute(self, sql: str, *params: Any) -> str:
        return "OK"

    async def fetchval(self, sql: str, *params: Any) -> Any:
        if "relkind" in sql:
            return "v"
        return list(_COLUMNS)

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        if "unique" in sql.lower() or "indisunique" in sql:
            return []
        evaluations = sql.count(f'"public"."{self._db.view}"')
        # Yield to the loop once, as a real query does, so that concurrent
        # readers interleave.
        await asyncio.sleep(0)
        if self._db.fail:
            raise OSError("database went away")
        self._db.evaluations += evaluations
        self._db.clock.now += evaluations * _RECORDED_EVALUATION_SECONDS[self._db.view]
        return self._db.window()


def _source(clock: _Clock, database: _Database) -> TableRowSource:
    source = TableRowSource(environ={DEFAULT_DSN_ENV: "postgresql://unused/db"})
    source._derived_windows = DerivedWindowCache(
        ttl_seconds=DEFAULT_DERIVED_READ_TTL_SECONDS,
        max_stale_seconds=DEFAULT_DERIVED_READ_MAX_STALE_SECONDS,
        clock=clock,
    )

    async def _pool(cfg: ProjectionTableConfig) -> Any:
        return database

    source._pool = _pool  # type: ignore[method-assign]
    return source


async def _read(source: TableRowSource, cfg: ProjectionTableConfig) -> list[Any]:
    return await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=_TENANT)


_VIEWS = sorted(_RECORDED_EVALUATION_SECONDS)


@pytest.mark.parametrize("view", _VIEWS)
async def test_a_cold_read_evaluates_the_view_once(view: str) -> None:
    clock = _Clock()
    database = _Database(clock, view)
    source = _source(clock, database)
    cfg = _cfg(view)

    started = clock.now
    await _read(source, cfg)

    assert database.evaluations == 1
    assert clock.now - started == pytest.approx(_RECORDED_EVALUATION_SECONDS[view])
    assert clock.now - started < _DASHBOARD_TIMEOUT_SECONDS / 2


@pytest.mark.parametrize("view", _VIEWS)
async def test_a_read_inside_the_held_window_costs_the_database_nothing(
    view: str,
) -> None:
    clock = _Clock()
    database = _Database(clock, view)
    source = _source(clock, database)
    cfg = _cfg(view)
    await _read(source, cfg)

    before = (clock.now, database.evaluations)
    for _ in range(5):
        rows = await _read(source, cfg)

    assert (clock.now, database.evaluations) == before
    assert len(rows) == 1
    assert clock.now - before[0] < _BUDGET_SECONDS


async def test_every_overview_view_answers_inside_the_budget_once_one_read_has_run() -> (
    None
):
    """The recorded cold cost of each view, then the held read, against 2 s."""
    held_over_budget = []
    for view in _VIEWS:
        clock = _Clock()
        database = _Database(clock, view)
        source = _source(clock, database)
        cfg = _cfg(view)
        await _read(source, cfg)
        started = clock.now
        await _read(source, cfg)
        if clock.now - started >= _BUDGET_SECONDS:
            held_over_budget.append(view)
    assert held_over_budget == []


async def test_the_recursive_per_key_read_is_the_recorded_cost_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injected break: the relation is read as a table, as before the fix.

    The per-key skip scan then names the view three times, so a read charges
    three evaluations and the views the dashboard timed out on read over the
    budget again. This is the red the fix turns green.
    """
    monkeypatch.setattr(table_reader, "_DERIVED_RELKINDS", frozenset())
    over_budget = []
    for view in _VIEWS:
        clock = _Clock()
        database = _Database(clock, view)
        source = _source(clock, database)
        cfg = _cfg(view)
        started = clock.now
        await _read(source, cfg)
        await _read(source, cfg)
        assert database.evaluations == 6, "two reads, three evaluations each"
        if (clock.now - started) / 2 >= _BUDGET_SECONDS:
            over_budget.append(view)
    assert "projection_delegation_model_routing" in over_budget
    assert len(over_budget) >= 1


def test_the_before_record_is_the_dashboard_timeout_the_ticket_names() -> None:
    over = [v for v, s in _RECORDED_BEFORE_SECONDS.items() if s > _BUDGET_SECONDS]
    assert len(over) == 4, "four views read over 2 s even at their best of three"
    assert max(_RECORDED_BEFORE_SECONDS.values()) > _DASHBOARD_TIMEOUT_SECONDS


async def test_a_read_past_the_ttl_answers_the_held_rows_and_refreshes() -> None:
    view = "projection_delegation_summary"
    clock = _Clock()
    database = _Database(clock, view)
    source = _source(clock, database)
    cfg = _cfg(view)
    await _read(source, cfg)
    database.total = 2
    clock.now += DEFAULT_DERIVED_READ_TTL_SECONDS + 1

    started = clock.now
    stale = await _read(source, cfg)

    assert stale[0]["total"] == 1, "the held rows answer; no wait for the refresh"
    assert clock.now == started
    for _ in range(5):
        await asyncio.sleep(0)
    assert database.evaluations == 2
    assert (await _read(source, cfg))[0]["total"] == 2


async def test_a_read_past_the_stale_bound_waits_for_the_refresh() -> None:
    view = "projection_delegation_summary"
    clock = _Clock()
    database = _Database(clock, view)
    source = _source(clock, database)
    cfg = _cfg(view)
    await _read(source, cfg)
    database.total = 3
    clock.now += DEFAULT_DERIVED_READ_MAX_STALE_SECONDS + 1

    rows = await _read(source, cfg)

    assert rows[0]["total"] == 3


async def test_concurrent_readers_of_a_cold_view_share_one_evaluation() -> None:
    view = "projection_delegation_savings"
    clock = _Clock()
    database = _Database(clock, view)
    source = _source(clock, database)
    cfg = _cfg(view)

    answers = await asyncio.gather(*(_read(source, cfg) for _ in range(8)))

    assert database.evaluations == 1
    assert all(len(rows) == 1 for rows in answers)


async def test_a_failed_refresh_keeps_the_held_rows_and_the_next_read_retries() -> None:
    view = "projection_delegation_summary"
    clock = _Clock()
    database = _Database(clock, view)
    source = _source(clock, database)
    cfg = _cfg(view)
    await _read(source, cfg)
    clock.now += DEFAULT_DERIVED_READ_TTL_SECONDS + 1
    database.fail = True

    held = await _read(source, cfg)
    for _ in range(5):
        await asyncio.sleep(0)

    assert held[0]["total"] == 1
    database.fail = False
    database.total = 5
    await _read(source, cfg)
    for _ in range(5):
        await asyncio.sleep(0)
    assert (await _read(source, cfg))[0]["total"] == 5


async def test_a_table_is_never_held() -> None:
    """A real table keeps the skip scan and answers fresh on every read."""
    view = "projection_delegation_summary"
    clock = _Clock()
    database = _Database(clock, view)
    source = _source(clock, database)
    cfg = _cfg(view)

    async def table_kind(self_cfg: ProjectionTableConfig) -> str:
        return "r"

    source._relation_kind = table_kind  # type: ignore[assignment]
    await _read(source, cfg)
    await _read(source, cfg)

    assert database.evaluations == 6


async def test_a_ttl_of_zero_holds_nothing() -> None:
    view = "projection_delegation_summary"
    clock = _Clock()
    database = _Database(clock, view)
    source = _source(clock, database)
    source._derived_windows = DerivedWindowCache(
        ttl_seconds=0, max_stale_seconds=0, clock=clock
    )
    cfg = _cfg(view)

    await _read(source, cfg)
    await _read(source, cfg)

    assert database.evaluations == 2


def test_a_view_is_read_in_one_pass_and_a_table_keeps_the_skip_scan() -> None:
    cfg = _cfg("projection_delegation_summary")
    columns = frozenset(_COLUMNS)
    derived = table_reader.build_window_query(
        cfg,
        order_spec=cfg.order_by_spec,
        tenant_id=_TENANT,
        relation_columns=columns,
        derived_relation=True,
    ).sql
    table = table_reader.build_window_query(
        cfg,
        order_spec=cfg.order_by_spec,
        tenant_id=_TENANT,
        relation_columns=columns,
    ).sql
    assert derived.count('"public"."projection_delegation_summary"') == 1
    assert "DISTINCT ON" in derived
    assert "WITH RECURSIVE" not in derived
    assert table.count('"public"."projection_delegation_summary"') == 3
    assert "WITH RECURSIVE" in table
