# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24458: a windowed projection read against a real PostgreSQL.

On the dev lane (2026-11-23) the overview's read of ``delegation.decisions``
ran the ascending walk: a parallel sequential scan of the tenant's 137,818 rows
that kept the OLDEST 2,000, whatever window the page showed. What only a real
database proves:

* W1 a ``window=7d`` page holds only rows whose ``written_at`` lies inside the
  last seven days by the database's own clock, newest first;
* W2 a ``window=24h`` page reads fewer rows than a ``window=7d`` page, and the
  rows read are the rows inside the window, not the history;
* W3 a row with no ``written_at`` is never served by a windowed read;
* W4 the same request with no window is still the ascending walk (the control).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.read_page import read_projection_page
from omnimarket.projection.table_reader import DEFAULT_DSN_ENV, TableRowSource
from tests.test_omn15359_ac3_replay_real_postgres import local_postgres

_SCHEMA = "omn24458_window"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_HOURS = 20 * 24
_UNDATED = 5


def _cfg() -> ProjectionTableConfig:
    columns = ("correlation_id", "tenant_id", "written_at", "projection_cursor")
    return ProjectionTableConfig(
        topic="onex.snapshot.projection.omn24458.decisions.v1",
        table="delegation_events",
        schema_name="public",
        relation_schema=_SCHEMA,
        columns=columns,
        order_by="written_at DESC",
        order_by_spec=parse_order_by_clauses("written_at DESC", columns),
        freshness_column="written_at",
        cursor_column="projection_cursor",
        limit=500,
        bus_backed=True,
        key_columns=("correlation_id",),
        key_grain="mutable",
        tenant_column="tenant_id",
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
    pg = request.getfixturevalue(local_postgres.__name__)[0]
    return f"postgresql://postgres@/{pg.database}?host={pg.host}"


@pytest.fixture
async def seeded(dsn: str) -> AsyncIterator[None]:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        # delegation_events' shape: a unique correlation_id, a cursor, and a
        # written_at the writer may leave empty. One run an hour for twenty
        # days, each half an hour inside its hour so no row sits on a boundary.
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.delegation_events ("
            " projection_cursor bigserial PRIMARY KEY,"
            " correlation_id text NOT NULL UNIQUE, tenant_id text NOT NULL,"
            " written_at timestamptz)"
        )
        await conn.execute(
            f"CREATE INDEX ON {_SCHEMA}.delegation_events (written_at DESC)"
        )
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.delegation_events"
            " (correlation_id, tenant_id, written_at) VALUES ($1, $2, $3)",
            [
                (
                    f"run-{n:05d}",
                    _TENANT,
                    datetime.now(UTC) - timedelta(hours=n, minutes=30),
                )
                for n in range(_HOURS)
            ],
        )
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.delegation_events"
            " (correlation_id, tenant_id, written_at) VALUES ($1, $2, NULL)",
            [(f"undated-{n}", _TENANT) for n in range(_UNDATED)],
        )
        yield
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


async def _page(dsn: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    cfg = _cfg()
    source = TableRowSource(environ={DEFAULT_DSN_ENV: dsn})
    try:
        page = await read_projection_page(
            cfg.topic,
            topic_map={cfg.topic: cfg},
            source=source,
            tenant=_TENANT,
            **kwargs,
        )
    finally:
        await source.close()
    return page.status_code, page.body


@pytest.mark.integration
async def test_w1_a_seven_day_page_holds_only_rows_inside_the_window(
    seeded: None, dsn: str
) -> None:
    status, body = await _page(dsn, window="7d", limit=500)

    assert status == 200, body
    floor = datetime.now(UTC) - timedelta(days=7)
    served = [datetime.fromisoformat(row["written_at"]) for row in body["rows"]]
    assert len(served) == 7 * 24
    assert all(stamp >= floor for stamp in served)
    assert served == sorted(served, reverse=True)
    assert body["window"]["rows_read"] == 7 * 24
    assert body["truncated"] is False


@pytest.mark.integration
async def test_w2_a_day_reads_fewer_rows_than_a_week_and_not_the_history(
    seeded: None, dsn: str
) -> None:
    _, day = await _page(dsn, window="24h", limit=500)
    _, week = await _page(dsn, window="7d", limit=500)

    assert day["window"]["rows_read"] == 24
    assert week["window"]["rows_read"] == 7 * 24
    assert week["window"]["rows_read"] < _HOURS


@pytest.mark.integration
async def test_w3_a_row_with_no_written_at_is_never_served_by_a_window(
    seeded: None, dsn: str
) -> None:
    _, body = await _page(dsn, window="30d", limit=500)

    assert all(row["written_at"] is not None for row in body["rows"])
    assert not any(
        str(row["correlation_id"]).startswith("undated") for row in body["rows"]
    )


@pytest.mark.integration
async def test_w4_the_same_request_with_no_window_is_still_the_walk(
    seeded: None, dsn: str
) -> None:
    _, body = await _page(dsn, limit=3)

    assert "window" not in body
    assert [row["projection_cursor"] for row in body["rows"]][:1] == [1] or sorted(
        row["projection_cursor"] for row in body["rows"]
    ) == [1, 2, 3]
