# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof of the ``window`` read over a TEXT freshness column (OMN-21288).

``public.metering_summary.as_of`` is TEXT (migration 0000). The windowed read
compared it with ``now() - interval``, so every ``window=7d`` read of the
metering-summary projection failed with ``operator does not exist: text >=
timestamp with time zone`` and answered 503. The table is built here from the
node's own migration chain, so the column types are the live ones.

WHICH DATABASE: as in the OMN-21970 module, CI's PostgreSQL 16 or an owned
container (skipped when docker is absent).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.read_page import read_projection_page
from omnimarket.projection.table_reader import TableRowSource
from tests.test_omn21970_daily_and_cursor_real_postgres import (
    _ROLES_SQL,
    _Postgres,
    postgres,  # noqa: F401  (module fixture)
)

pytestmark = pytest.mark.integration

_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_metering_summary/migrations"
)
_TOPIC = "onex.snapshot.projection.metering-summary.v1"
_TENANT = "0af28cd0-0654-42e0-a050-358ee8240940"
_DAY = 86400.0


@asynccontextmanager
async def _provisioned(pg: _Postgres) -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    admin = await asyncpg.connect(**pg.connect_kwargs())
    schema = f"omn21288_{uuid4().hex[:12]}"
    try:
        await admin.execute(_ROLES_SQL)
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}, public")
        for migration in sorted(_MIGRATIONS.glob("*.sql")):
            await admin.execute(
                migration.read_text(encoding="utf-8").replace("public.", f"{schema}.")
            )
        yield admin, schema
    finally:
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute("SET search_path TO public")
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


async def _insert(admin: asyncpg.Connection, key: str, as_of: str) -> None:
    await admin.execute(
        "INSERT INTO metering_summary (tenant_id, window_kind, window_start, "
        "window_end, as_of, baseline_model, baseline_state, runs_total, "
        "runs_measured, runs_unknown_tokens, runs_unknown_spend, tokens_in, "
        "tokens_out, summary_json) VALUES ($1, 'day', $2, $2, $3, 'm', 'resolved', "
        "1, 1, 0, 0, 1, 1, '{}')",
        _TENANT,
        key,
        as_of,
    )


async def _window(pg: _Postgres, schema: str, window: str) -> dict[str, object]:
    cfg = build_projection_topic_map()[_TOPIC].model_copy(
        update={"relation_schema": schema}
    )
    source = TableRowSource.for_database_url(pg.dsn(schema))
    try:
        page = await read_projection_page(
            _TOPIC,
            topic_map={_TOPIC: cfg},
            source=source,
            tenant=_TENANT,
            window=window,
        )
    finally:
        await source.close()
    assert page.status_code == 200, page.body
    return page.body


@pytest.mark.asyncio
async def test_window_reads_text_timestamps_and_names_undated_rows(
    postgres: _Postgres,  # noqa: F811
) -> None:
    now = datetime.now(UTC)
    ages_days = {"d0": 0.5, "d3": 3.0, "d6": 6.5, "d8": 8.0, "d20": 20.0, "d40": 40.0}
    async with _provisioned(postgres) as (admin, schema):
        for key, age in ages_days.items():
            await _insert(admin, key, (now - timedelta(days=age)).isoformat())
        await _insert(admin, "bad-text", "not-a-timestamp")
        await _insert(admin, "bad-empty", "")
        await _insert(admin, "bad-calendar", "2026-02-30T00:00:00+00:00")
        seen = {
            window: await _window(postgres, schema, window)
            for window in ("1d", "7d", "30d")
        }

    def keys(window: str) -> set[str]:
        rows = seen[window]["rows"]
        assert isinstance(rows, list)
        return {row["window_start"] for row in rows}

    assert keys("1d") == {"d0"}
    assert keys("7d") == {"d0", "d3", "d6"}
    assert keys("30d") == {"d0", "d3", "d6", "d8", "d20"}
    for window in seen:
        window_body = seen[window]["window"]
        assert isinstance(window_body, dict)
        assert window_body["undated_rows"] == 3


@pytest.mark.asyncio
async def test_window_survives_values_that_match_the_shape_but_are_not_instants(
    postgres: _Postgres,  # noqa: F811
) -> None:
    """A text value that looks like an instant but is not one is undated, never a 503.

    Feb 29 of a non-leap year (2026, and 2100 which is not a leap century), year
    0000 and an offset past the zone limit all fit an ISO-8601 shape and all make
    ``::timestamptz`` raise; a leap day (2024, 2000) is a real instant.
    """
    now = datetime.now(UTC)
    unreadable = {
        "bad-feb29-2026": "2026-02-29T00:00:00+00:00",
        "bad-feb29-2100": "2100-02-29T12:00:00Z",
        "bad-year-0000": "0000-01-01T00:00:00Z",
        "bad-offset-23h": "2026-06-15T00:00:00+23:00",
    }
    leap_days = {
        "ok-leap-2024": "2024-02-29T12:00:00+00:00",
        "ok-leap-2000": "2000-02-29 12:00:00Z",
    }
    async with _provisioned(postgres) as (admin, schema):
        await _insert(admin, "d0", (now - timedelta(days=0.5)).isoformat())
        for key, as_of in {**unreadable, **leap_days}.items():
            await _insert(admin, key, as_of)
        page = await _window(postgres, schema, "7d")
    rows = page["rows"]
    assert isinstance(rows, list)
    assert {row["window_start"] for row in rows} == {"d0"}
    window_body = page["window"]
    assert isinstance(window_body, dict)
    assert window_body["undated_rows"] == len(unreadable)


@pytest.mark.asyncio
async def test_pattern_admits_exactly_the_calendar_dates_the_cast_accepts(
    postgres: _Postgres,  # noqa: F811
) -> None:
    """Every MM-DD of every year in a span: the guarded cast never raises, and the
    pattern passes a date exactly when the calendar has it."""
    from calendar import monthrange

    from omnimarket.projection.table_reader import _ISO_INSTANT_PATTERN

    candidates: list[str] = []
    real = 0
    for year in (
        0,
        1,
        1899,
        1900,
        1999,
        2000,
        2023,
        2024,
        2026,
        2027,
        2100,
        2400,
        9999,
    ):
        for month in range(1, 13):
            for day in range(1, 32):
                candidates.append(f"{year:04d}-{month:02d}-{day:02d}T00:00:00Z")
                if year >= 1 and day <= monthrange(year, month)[1]:
                    real += 1
    admin = await asyncpg.connect(**postgres.connect_kwargs())
    try:
        # The same shape the reader builds: the cast is reached only through the CASE.
        cast_ok = await admin.fetchval(
            "SELECT count(CASE WHEN s ~ $2 THEN s::timestamptz END) "
            "FROM unnest($1::text[]) AS s",
            candidates,
            _ISO_INSTANT_PATTERN,
        )
    finally:
        await admin.close()
    assert cast_ok == real
