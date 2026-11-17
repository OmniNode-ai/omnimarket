# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real Postgres tests for the done_items write path.

Catches: parameter binding and projected_at round-tripping; newer events
replacing older ones while older events do not; replayed events returning no
row; and malformed done_row_id values being rejected by the database.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.nodes.node_projection_done_items.handlers.handler_done_items_writer import (
    upsert_statement,
)

_PORT = 5432
_USER = "postgres"
_DB = "omnibase_infra"
_SCHEMA = "omn25104_done_items_write_path_test"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.getenv("INTEGRATION_POSTGRES_PASSWORD") or os.getenv(
        "POSTGRES_PASSWORD", ""
    )
    if not password:
        pytest.skip("postgres credentials not provided")
    host = os.getenv("INTEGRATION_POSTGRES_HOST", "localhost")
    try:
        return await asyncpg.connect(
            f"postgresql://{_USER}:{quote_plus(password)}@{host}:{_PORT}/{_DB}"
        )
    except (OSError, asyncpg.PostgresError):
        pytest.skip("postgres unavailable")


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    migration_path = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_projection_done_items/migrations/0000_create_done_items.sql"
    )
    sql = migration_path.read_text()
    await conn.execute(sql.replace("omninode_internal.", f"{_SCHEMA}."))


statement = upsert_statement(f"{_SCHEMA}.done_items")


async def _teardown(conn: asyncpg.Connection) -> None:
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    finally:
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_done_item_binds_and_round_trips() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        projected_at = datetime(2026, 11, 17, 10, 0, 5, tzinfo=UTC)

        row = await conn.fetchrow(
            statement,
            "OMN-1",
            "2026-11-17T10:00:00Z",
            "a" * 64,
            "alpha",
            None,
            None,
            projected_at,
        )
        assert row is not None

        record = await conn.fetchrow(
            f"SELECT projected_at FROM {_SCHEMA}.done_items WHERE ticket = $1",
            "OMN-1",
        )
        assert isinstance(record["projected_at"], datetime)
        assert record["projected_at"] == projected_at
    finally:
        await _teardown(conn)


@pytest.mark.integration
async def test_real_postgres_newer_done_event_replaces_older_does_not() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)

        await conn.fetchrow(
            statement,
            "OMN-1",
            "2026-11-17T10:00:00Z",
            "a" * 64,
            "alpha",
            None,
            None,
            datetime(2026, 11, 17, 10, 0, 5, tzinfo=UTC),
        )
        newer = await conn.fetchrow(
            statement,
            "OMN-1",
            "2026-11-17T11:00:00Z",
            "b" * 64,
            "alpha",
            None,
            None,
            datetime(2026, 11, 17, 11, 0, 5, tzinfo=UTC),
        )
        assert newer is not None

        older = await conn.fetchrow(
            statement,
            "OMN-1",
            "2026-11-17T10:30:00Z",
            "c" * 64,
            "alpha",
            None,
            None,
            datetime(2026, 11, 17, 10, 30, 5, tzinfo=UTC),
        )
        assert older is None

        final = await conn.fetchrow(
            f"SELECT done_at, done_row_id FROM {_SCHEMA}.done_items WHERE ticket = $1",
            "OMN-1",
        )
        assert final["done_at"] == "2026-11-17T11:00:00Z"
        assert final["done_row_id"] == "b" * 64
    finally:
        await _teardown(conn)


@pytest.mark.integration
async def test_real_postgres_same_event_replayed_returns_no_row() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        params = (
            "OMN-1",
            "2026-11-17T10:00:00Z",
            "a" * 64,
            "alpha",
            None,
            None,
            datetime(2026, 11, 17, 10, 0, 5, tzinfo=UTC),
        )

        await conn.fetch(statement, *params)
        second = await conn.fetch(statement, *params)
        assert second == []

        count = await conn.fetchval(f"SELECT COUNT(*) FROM {_SCHEMA}.done_items")
        assert count == 1
    finally:
        await _teardown(conn)


@pytest.mark.integration
async def test_real_postgres_refuses_a_malformed_row_id() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.fetch(
                statement,
                "OMN-1",
                "2026-11-17T10:00:00Z",
                "not-a-sha",
                "alpha",
                None,
                None,
                datetime(2026, 11, 17, 10, 0, 5, tzinfo=UTC),
            )
    finally:
        await _teardown(conn)
