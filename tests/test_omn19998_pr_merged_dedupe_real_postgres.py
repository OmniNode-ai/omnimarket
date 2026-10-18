# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof of the pr_merged_events (repo, pr_number) dedupe (OMN-19998).

The golden-chain module proves the handler against the in-memory adapter and
reads migration 0003 as text. Here migration 0001 builds the table in a
throwaway schema, duplicate rows are seeded the way a fresh ``event_id`` replay
leaves them, migration 0003 runs, and the surviving rows are read back. The
handler then writes through the sync Postgres adapter the deployed runtime uses.

The integration_postgres_dsn fixture reads the INTEGRATION_POSTGRES host, port,
user and database settings from tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.nodes.node_pr_merged_projection.handlers.handler_pr_merged_projection import (
    HandlerPrMergedProjection,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter

pytestmark = pytest.mark.integration

_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_merged_projection"
    / "migrations"
)
_CREATE = _MIGRATIONS / "0001_create_pr_merged_events.sql"
_DEDUPE = _MIGRATIONS / "0003_pr_merged_events_unique_repo_pr_number.sql"

_INSERT = (
    "INSERT INTO pr_merged_events (event_id, repo, branch, pr_number, merged_at) "
    "VALUES ($1, $2, 'b', $3, '2026-06-18T12:00:00+00:00')"
)
_UNIQUE_ON_REPO_PR = (
    "SELECT count(*) FROM pg_constraint c WHERE c.conrelid = 'pr_merged_events'::regclass "
    "AND c.contype = 'u' AND (SELECT array_agg(a.attname::text ORDER BY a.attname) "
    "FROM unnest(c.conkey) AS k(attnum) JOIN pg_attribute a "
    "ON a.attrelid = c.conrelid AND a.attnum = k.attnum) = ARRAY['pr_number', 'repo']"
)


@asynccontextmanager
async def _table_at_0001(dsn: str) -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    conn = await asyncpg.connect(dsn)
    schema = f"omn19998_{uuid4().hex[:12]}"
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute(_CREATE.read_text(encoding="utf-8"))
        yield conn, schema
    finally:
        with contextlib.suppress(asyncpg.PostgresError):
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


async def test_the_migration_keeps_the_first_row_of_each_repo_pr(
    integration_postgres_dsn: str,
) -> None:
    async with _table_at_0001(integration_postgres_dsn) as (conn, _):
        for event_id, repo, pr_number in (
            ("evt-a1", "OmniNode-ai/a", 1),
            ("evt-a1-replay", "OmniNode-ai/a", 1),
            ("evt-a1-replay-2", "OmniNode-ai/a", 1),
            ("evt-a2", "OmniNode-ai/a", 2),
            ("evt-b1", "OmniNode-ai/b", 1),
        ):
            await conn.execute(_INSERT, event_id, repo, pr_number)

        await conn.execute(_DEDUPE.read_text(encoding="utf-8"))

        kept = await conn.fetch(
            "SELECT event_id FROM pr_merged_events ORDER BY projection_cursor"
        )
        assert [row["event_id"] for row in kept] == ["evt-a1", "evt-a2", "evt-b1"]
        assert await conn.fetchval(_UNIQUE_ON_REPO_PR) == 1
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(_INSERT, "evt-a1-late", "OmniNode-ai/a", 1)


async def test_the_migration_is_idempotent(integration_postgres_dsn: str) -> None:
    async with _table_at_0001(integration_postgres_dsn) as (conn, _):
        await conn.execute(_INSERT, "evt-a1", "OmniNode-ai/a", 1)
        await conn.execute(_DEDUPE.read_text(encoding="utf-8"))
        await conn.execute(_DEDUPE.read_text(encoding="utf-8"))
        assert await conn.fetchval(_UNIQUE_ON_REPO_PR) == 1
        assert await conn.fetchval("SELECT count(*) FROM pr_merged_events") == 1


async def test_the_handler_dedupes_a_fresh_event_id_through_the_sync_adapter(
    integration_postgres_dsn: str,
) -> None:
    async with _table_at_0001(integration_postgres_dsn) as (conn, schema):
        await conn.execute(_DEDUPE.read_text(encoding="utf-8"))
        adapter = PostgresSyncProjectionAdapter(integration_postgres_dsn, schema=schema)
        handler = HandlerPrMergedProjection()

        def _project(event_id: str, repo: str) -> None:
            handler.project(
                ModelPrMergedEvent(
                    event_id=event_id,
                    repo=repo,
                    branch="feature/x",
                    pr_number=7,
                    ticket="OMN-19998",
                    merged_at="2026-06-18T12:00:00+00:00",
                ),
                adapter,
            )

        await asyncio.to_thread(_project, "evt-first", "OmniNode-ai/a")
        await asyncio.to_thread(_project, "evt-replay", "OmniNode-ai/a")
        await asyncio.to_thread(_project, "evt-other-repo", "OmniNode-ai/b")

        rows = await conn.fetch(
            "SELECT repo, pr_number FROM pr_merged_events ORDER BY projection_cursor"
        )
        assert [(row["repo"], row["pr_number"]) for row in rows] == [
            ("OmniNode-ai/a", 7),
            ("OmniNode-ai/b", 7),
        ]
