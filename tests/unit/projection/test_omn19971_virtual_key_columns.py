# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19971: a declared key column the relation does not carry never reaches SQL.

Six snapshot exposures (delegation summary, model-routing, quality-gate,
token-usage, savings, and cost savings-overview) key on
``(snapshot_grain, tenant_id)``. ``snapshot_grain`` is the constant half of the
bus compaction key, minted by the republish query and deliberately not a column
of the view. The latest-row-per-key read added by OMN-20327 selected every
declared key column from the relation, so all six answered 503
``projection_column_missing`` on the dev lane (2026-10-02 06:01Z readback).

Failure modes these tests are written against:

* F1 a virtual key column reaches the SQL text;
* F2 a key column the relation DOES carry is dropped, collapsing distinct keys
  (the consumer-flow defect OMN-20327 fixed);
* F3 every key column is virtual and the per-key path runs with no keys;
* F5 the relation's columns are read with an interpolated relation name, or
  read again on every request.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import (
    DEFAULT_DSN_ENV,
    TableRowSource,
    build_window_query,
)

_SAVINGS = "onex.snapshot.projection.delegation.savings.v1"
_FLOW = "onex.snapshot.projection.consumer-flow.v1"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_VIEW_COLUMNS = frozenset(
    {"tenant_id", "cumulative_savings_usd", "latest_projection_updated_at"}
)


def _savings_cfg(**overrides: Any) -> ProjectionTableConfig:
    columns = ("tenant_id", "cumulative_savings_usd", "latest_projection_updated_at")
    fields: dict[str, Any] = {
        "topic": _SAVINGS,
        "table": "projection_delegation_savings",
        "schema_name": "public",
        "relation_schema": "public",
        "columns": columns,
        "order_by": "latest_projection_updated_at DESC",
        "order_by_spec": parse_order_by_clauses(
            "latest_projection_updated_at DESC", columns
        ),
        "freshness_column": "latest_projection_updated_at",
        "limit": 50,
        "bus_backed": True,
        "key_columns": ("snapshot_grain", "tenant_id"),
        "tenant_column": "tenant_id",
    }
    fields.update(overrides)
    return ProjectionTableConfig(**fields)


def _flow_cfg() -> ProjectionTableConfig:
    columns = ("projection_cursor", "consumer_group", "topic", "window_end")
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
        limit=50,
        bus_backed=True,
        key_columns=("consumer_group", "topic"),
    )


def _query(cfg: ProjectionTableConfig, relation_columns: frozenset[str] | None) -> str:
    return build_window_query(
        cfg,
        order_spec=cfg.order_by_spec,
        tenant_id=_TENANT if cfg.tenant_column else None,
        relation_columns=relation_columns,
    ).sql


def test_a_virtual_key_column_never_reaches_the_sql() -> None:
    sql = _query(_savings_cfg(), _VIEW_COLUMNS)
    assert "snapshot_grain" not in sql
    assert "WITH RECURSIVE" in sql, "tenant_id is a real key; the per-key read stays"
    assert '"tenant_id"' in sql


def test_keys_the_relation_carries_are_all_kept() -> None:
    cfg = _flow_cfg()
    every_column = frozenset({*cfg.columns})
    assert _query(cfg, every_column) == _query(cfg, None)
    sql = _query(cfg, every_column)
    assert '"consumer_group"' in sql
    assert '"topic"' in sql


def test_all_virtual_keys_skip_the_per_key_read() -> None:
    sql = _query(_savings_cfg(key_columns=("snapshot_grain",)), _VIEW_COLUMNS)
    assert "WITH RECURSIVE" not in sql
    assert "snapshot_grain" not in sql


# ---------------------------------------------------------------------------
# Through the Postgres row source, over a fake connection
# ---------------------------------------------------------------------------


class _Connection:
    def __init__(self, log: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._log = log

    @asynccontextmanager
    async def transaction(self, *, readonly: bool = False) -> Any:
        yield

    async def execute(self, sql: str, *params: Any) -> str:
        self._log.append((sql, params))
        return "OK"

    async def fetchval(self, sql: str, *params: Any) -> Any:
        self._log.append((sql, params))
        return sorted(_VIEW_COLUMNS)

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self._log.append((sql, params))
        return []


class _Pool:
    def __init__(self) -> None:
        self.log: list[tuple[str, tuple[Any, ...]]] = []

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield _Connection(self.log)


def _source(pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> TableRowSource:
    source = TableRowSource(environ={DEFAULT_DSN_ENV: "postgresql://unused/db"})

    async def _pool(cfg: ProjectionTableConfig) -> Any:
        return pool

    monkeypatch.setattr(source, "_pool", _pool)
    return source


async def test_the_row_source_reads_the_relation_columns_bound_and_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    source = _source(pool, monkeypatch)
    cfg = _savings_cfg()

    for _ in range(2):
        await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=_TENANT)

    # The column lookup itself: the unique-index lookup also joins
    # pg_attribute (for each key column's collation) and is a separate read.
    lookups = [(sql, params) for sql, params in pool.log if "array_agg(attname" in sql]
    assert len(lookups) == 1, "the relation's columns are read once, then cached"
    sql, params = lookups[0]
    assert "projection_delegation_savings" not in sql, "the relation is a bound value"
    assert len(params) == 1
    assert "projection_delegation_savings" in params[0]
    reads = [sql for sql, _ in pool.log if sql.startswith("SELECT * FROM")]
    assert len(reads) == 2
    assert all("snapshot_grain" not in sql for sql in reads)
