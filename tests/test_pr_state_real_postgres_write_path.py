# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19999 real PostgreSQL 16 gate: DDL, bound types, ordering and idempotency.

Set INTEGRATION_POSTGRES_DSN explicitly. With no reachable database the gate
skips, like the work-ledger reference. Every test owns a throwaway schema; no
runtime tables or roles are changed. The test never starts a service.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.events.pr_state import (
    EnumPrState,
    ModelPrStateObservedEvent,
)
from omnimarket.nodes.node_projection_pr_state.handlers import (
    handler_pr_state_projection as writer_module,
)
from omnimarket.nodes.node_projection_pr_state.handlers.handler_pr_state_projection import (
    PrStateProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta
from tests.unit.nodes.node_pr_state_emit_effect.helpers import event

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_pr_state/migrations/0000_create_pr_state.sql"
)


async def _connect_or_skip() -> asyncpg.Connection:
    if "INTEGRATION_POSTGRES_DSN" not in os.environ:
        pytest.skip("INTEGRATION_POSTGRES_DSN is not configured for OMN-19999")
    dsn = os.environ["INTEGRATION_POSTGRES_DSN"]
    if not dsn.strip():
        raise ValueError("INTEGRATION_POSTGRES_DSN is empty")
    try:
        connection = await asyncpg.connect(dsn, timeout=5)
    except (OSError, TimeoutError, asyncpg.PostgresError):
        pytest.skip("no reachable Postgres for the OMN-19999 write-path gate")
    else:
        if connection.get_server_version().major != 16:
            await connection.close()
            pytest.skip("the OMN-19999 write-path gate requires PostgreSQL 16")
        return connection


class ConnectionDb:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def execute(self, sql: str, *args: object) -> None:
        await self.connection.execute(sql, *args)


@asynccontextmanager
async def migrated() -> AsyncIterator[
    tuple[PrStateProjectionWriter, asyncpg.Connection, str]
]:
    connection = await _connect_or_skip()
    schema = f"omn19999_{uuid4().hex[:12]}"
    original = writer_module._UPSERT
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        ddl = MIGRATION.read_text().replace("omninode_internal.", f"{schema}.")
        await connection.execute(ddl)
        await connection.execute(ddl)  # Migration rerun is safe.
        writer = PrStateProjectionWriter()
        writer._db = ConnectionDb(connection)  # type: ignore[assignment]
        writer_module._UPSERT = original.replace("omninode_internal.", f"{schema}.")
        yield writer, connection, schema
    finally:
        writer_module._UPSERT = original
        try:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await connection.close()


async def project(
    writer: PrStateProjectionWriter, e: ModelPrStateObservedEvent
) -> None:
    topic = writer.subscribe_topics[0]
    await writer.project_event(
        topic,
        e.model_dump(mode="json"),
        MessageMeta(partition=0, offset=1, fallback_id=e.digest, topic=topic),
    )


@pytest.mark.integration
async def test_ddl_and_all_bound_fields_survive_a_merged_lifecycle() -> None:
    async with migrated() as (writer, connection, schema):
        await project(writer, event())
        merged = event(
            state=EnumPrState.MERGED,
            observed_at="2026-09-28T11:00:00Z",
            merged_at="2026-09-28T10:59:00Z",
        )
        await project(writer, merged)
        row = await connection.fetchrow(f"SELECT * FROM {schema}.pr_state")
        assert row is not None
        assert row["state"] == "merged"
        assert row["last_digest"] == row["digest"] == merged.digest
        assert row["observed_at"].isoformat() == "2026-09-28T11:00:00+00:00"
        assert set(row) == set(type(merged).model_fields) | {"last_digest"}


@pytest.mark.integration
async def test_older_event_cannot_overwrite_in_sql() -> None:
    async with migrated() as (writer, connection, schema):
        newer = event(head_sha="b" * 40, observed_at="2026-09-28T11:00:00Z")
        await project(writer, newer)
        await project(writer, event())
        assert (
            await connection.fetchval(f"SELECT head_sha FROM {schema}.pr_state")
            == newer.head_sha
        )


@pytest.mark.integration
async def test_equal_timestamp_tie_breaks_on_digest_in_both_orders() -> None:
    async with migrated() as (writer, connection, schema):
        low, high = sorted([event(), event(armed=True)], key=lambda e: e.digest)
        for first, second in ((low, high), (high, low)):
            await connection.execute(f"TRUNCATE {schema}.pr_state")
            await project(writer, first)
            await project(writer, second)
            assert (
                await connection.fetchval(f"SELECT last_digest FROM {schema}.pr_state")
                == high.digest
            )


@pytest.mark.integration
async def test_identical_redelivery_does_not_update_the_physical_row() -> None:
    async with migrated() as (writer, connection, schema):
        await project(writer, event())
        before = await connection.fetchrow(
            f"SELECT ctid, xmin, * FROM {schema}.pr_state"
        )
        await project(writer, event())
        after = await connection.fetchrow(
            f"SELECT ctid, xmin, * FROM {schema}.pr_state"
        )
        assert before == after
        assert await connection.fetchval(f"SELECT count(*) FROM {schema}.pr_state") == 1


@pytest.mark.integration
async def test_same_digest_new_tick_advances_and_closed_row_is_retained() -> None:
    async with migrated() as (writer, connection, schema):
        await project(writer, event())
        await project(writer, event(observed_at="2026-09-28T11:00:00Z"))
        assert (
            await connection.fetchval(f"SELECT observed_at FROM {schema}.pr_state")
        ).hour == 11
        await project(
            writer, event(state=EnumPrState.CLOSED, observed_at="2026-09-28T12:00:00Z")
        )
        await project(writer, event())
        assert (
            await connection.fetchval(f"SELECT state FROM {schema}.pr_state")
            == "closed"
        )
