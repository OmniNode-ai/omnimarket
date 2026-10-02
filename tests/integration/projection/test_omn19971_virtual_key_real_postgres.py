# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19971: the Postgres row source against a real, disposable PostgreSQL.

The six delegation and savings aggregates key on ``(snapshot_grain,
tenant_id)``, and ``snapshot_grain`` is not a column of their views. On the
dev lane all six answered 503 ``projection_column_missing`` after OMN-20327's
latest-row-per-key read (2026-10-02 06:01Z). What only a real database proves:

* F1 the view without ``snapshot_grain`` is served, one row per tenant;
* F2 a table whose keys are all real still serves the newest row of EVERY key
  (consumer-flow's shape, the defect OMN-20327 fixed);
* F3 a view whose only key is virtual is served as a plain window;
* F4 a declared non-key column the relation lacks is still refused by name;
* F5 a relation that does not exist is still refused by name.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import (
    DEFAULT_DSN_ENV,
    ProjectionReadError,
    TableRowSource,
)
from tests.test_omn15359_ac3_replay_real_postgres import local_postgres

# The disposable-PostgreSQL fixture the ``dsn`` fixture falls back to.
_FIXTURES = (local_postgres,)
_SCHEMA = "omn19971"
_TENANT_A = "820272f9-4aaf-5add-a2df-0af942852ab2"
_TENANT_B = "11111111-2222-4333-8444-555555555555"
_SAVINGS_COLUMNS = (
    "tenant_id",
    "cumulative_savings_usd",
    "latest_projection_updated_at",
)


def _savings_cfg(
    table: str = "projection_delegation_savings", **overrides: Any
) -> ProjectionTableConfig:
    columns = overrides.pop("columns", _SAVINGS_COLUMNS)
    fields: dict[str, Any] = {
        "topic": f"onex.snapshot.projection.omn19971.{table}.v1",
        "table": table,
        "schema_name": "public",
        "relation_schema": _SCHEMA,
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
        topic="onex.snapshot.projection.omn19971.consumer-flow.v1",
        table="consumer_flow_windows",
        schema_name="public",
        relation_schema=_SCHEMA,
        columns=columns,
        order_by="window_end DESC",
        order_by_spec=parse_order_by_clauses("window_end DESC", columns),
        freshness_column="window_end",
        cursor_column="projection_cursor",
        limit=50,
        bus_backed=True,
        key_columns=("consumer_group", "topic"),
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
    pg = request.getfixturevalue("local_postgres")[0]
    return f"postgresql://postgres@/{pg.database}?host={pg.host}"


@pytest.fixture
async def seeded(dsn: str) -> AsyncIterator[None]:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        # The aggregate's storage, and the view the exposure reads: grouped per
        # tenant, with no snapshot_grain column (it exists only on the bus key).
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.savings_events ("
            " tenant_id text NOT NULL, saved_usd numeric NOT NULL,"
            " written_at timestamptz NOT NULL)"
        )
        for tenant, saved, minute in (
            (_TENANT_A, 1, 0),
            (_TENANT_A, 2, 1),
            (_TENANT_B, 5, 2),
        ):
            await conn.execute(
                f"INSERT INTO {_SCHEMA}.savings_events VALUES ($1, $2, $3)",
                tenant,
                saved,
                datetime(2026, 10, 2, 6, minute, tzinfo=UTC),
            )
        await conn.execute(
            f"CREATE VIEW {_SCHEMA}.projection_delegation_savings AS"
            " SELECT tenant_id, sum(saved_usd) AS cumulative_savings_usd,"
            " max(written_at) AS latest_projection_updated_at"
            f" FROM {_SCHEMA}.savings_events GROUP BY tenant_id"
        )
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.consumer_flow_windows ("
            " projection_cursor bigserial PRIMARY KEY, consumer_group text NOT NULL,"
            " topic text NOT NULL, window_end timestamptz NOT NULL)"
        )
        # Three keys; the busy one holds many windows, so a recency cut over the
        # table would miss the quiet keys. Every key must still be served.
        windows = [("busy", "t1", m) for m in range(20)] + [
            ("quiet", "t1", 0),
            ("quiet", "t2", 1),
        ]
        for group, topic, minute in windows:
            await conn.execute(
                f"INSERT INTO {_SCHEMA}.consumer_flow_windows"
                " (consumer_group, topic, window_end) VALUES ($1, $2, $3)",
                group,
                topic,
                datetime(2026, 10, 2, 5, minute, tzinfo=UTC),
            )
        yield
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


async def _rows(
    dsn: str, cfg: ProjectionTableConfig, tenant_id: str | None = None
) -> list[dict[str, Any]]:
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        return await source.rows(cfg, order_spec=cfg.order_by_spec, tenant_id=tenant_id)
    finally:
        await source.close()


@pytest.mark.integration
async def test_a_view_without_the_virtual_key_is_served_per_tenant(
    seeded: None, dsn: str
) -> None:
    rows = await _rows(dsn, _savings_cfg(), tenant_id=_TENANT_A)
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == _TENANT_A
    assert str(rows[0]["cumulative_savings_usd"]) == "3"


@pytest.mark.integration
async def test_real_keys_still_serve_the_newest_row_of_every_key(
    seeded: None, dsn: str
) -> None:
    rows = await _rows(dsn, _flow_cfg())
    served = sorted((row["consumer_group"], row["topic"]) for row in rows)
    assert served == [("busy", "t1"), ("quiet", "t1"), ("quiet", "t2")]


@pytest.mark.integration
async def test_a_view_whose_only_key_is_virtual_is_a_plain_window(
    seeded: None, dsn: str
) -> None:
    rows = await _rows(
        dsn, _savings_cfg(key_columns=("snapshot_grain",)), tenant_id=_TENANT_B
    )
    assert [row["tenant_id"] for row in rows] == [_TENANT_B]


@pytest.mark.integration
async def test_a_missing_non_key_column_is_still_refused_by_name(
    seeded: None, dsn: str
) -> None:
    cfg = _savings_cfg(columns=(*_SAVINGS_COLUMNS, "not_a_column"))
    with pytest.raises(ProjectionReadError) as refused:
        await _rows(dsn, cfg, tenant_id=_TENANT_A)
    assert refused.value.code == "projection_column_missing"


@pytest.mark.integration
async def test_a_missing_relation_is_still_refused_by_name(
    seeded: None, dsn: str
) -> None:
    with pytest.raises(ProjectionReadError) as refused:
        await _rows(dsn, _savings_cfg(table="never_written"), tenant_id=_TENANT_A)
    assert refused.value.code == "projection_table_missing"
