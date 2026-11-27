# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres store of the outbox flush against a scratch PostgreSQL 16 holding the dev ``tenant_event_outbox`` shape.

The table is the one ``node_tenant_provision_effect`` writes (id, tenant_id, envelope, created_at, published_at), created
here in a schema of its own. A recording pool cannot tell whether the store's statements name columns the table has.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.handler_tenant_outbox_flush import (
    HandlerTenantOutboxFlush,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.tenant_outbox_postgres import (
    TenantOutboxPostgres,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.models.model_tenant_outbox_flush import (
    EnumTenantOutboxFlushOutcome,
    ModelTenantOutboxFlushCommand,
    ModelTenantOutboxFlushResult,
)
from tests.nodes.node_tenant_provision_effect.postgres_support import connect_kwargs

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 11, 25, 7, 0, tzinfo=UTC)

OUTBOX_DDL = """
CREATE SCHEMA {schema};
SET search_path TO {schema};
CREATE TABLE tenants (
    tenant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_slug text UNIQUE NOT NULL
);
CREATE TABLE tenant_event_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants (tenant_id),
    envelope jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
"""


@asynccontextmanager
async def identity_tables(
    port: int,
) -> AsyncIterator[tuple[asyncpg.Connection, asyncpg.Pool]]:
    """The dev outbox shape in a schema of its own, so no other test's tables share its name."""
    schema = f"outbox_flush_{uuid4().hex[:12]}"
    settings = {"search_path": schema}
    admin = await asyncpg.connect(**connect_kwargs(port), server_settings=settings)
    pool = await asyncpg.create_pool(
        **connect_kwargs(port), min_size=1, max_size=2, server_settings=settings
    )
    try:
        await admin.execute(OUTBOX_DDL.format(schema=schema))
        yield admin, pool
    finally:
        await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


async def _seed(admin: asyncpg.Connection, count: int) -> list[str]:
    tenant_id = await admin.fetchval(
        "INSERT INTO tenants (tenant_slug) VALUES ($1) RETURNING tenant_id",
        f"t-{uuid4().hex[:8]}",
    )
    ids: list[str] = []
    for index in range(count):
        envelope = {"operation": "TENANT_CREATED", "correlation_id": f"c{index}"}
        row_id = await admin.fetchval(
            "INSERT INTO tenant_event_outbox (tenant_id, envelope, created_at) "
            "VALUES ($1, $2::jsonb, now() + make_interval(secs => $3)) RETURNING id::text",
            tenant_id,
            json.dumps(envelope),
            float(index),
        )
        ids.append(row_id)
    return ids


async def test_fetch_returns_unpublished_rows_oldest_first(port: int) -> None:
    async with identity_tables(port) as (admin, pool):
        ids = await _seed(admin, 3)
        rows = await TenantOutboxPostgres(pool=pool).fetch_unpublished(10)
        assert [r.row_id for r in rows] == ids
        assert rows[0].envelope["correlation_id"] == "c0"


async def test_a_marked_row_is_not_fetched_again(port: int) -> None:
    async with identity_tables(port) as (admin, pool):
        ids = await _seed(admin, 2)
        store = TenantOutboxPostgres(pool=pool)
        await store.mark_published(ids[0], NOW)
        rows = await store.fetch_unpublished(10)
        assert [r.row_id for r in rows] == [ids[1]]
        stored = await admin.fetchval(
            "SELECT published_at FROM tenant_event_outbox WHERE id::text = $1", ids[0]
        )
        assert stored == NOW


async def test_a_failed_publish_leaves_the_row_unpublished_and_fetchable(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        ids = await _seed(admin, 1)
        store = TenantOutboxPostgres(pool=pool)
        await store.record_failure(ids[0], "KafkaTimeoutError: x")
        rows = await store.fetch_unpublished(10)
        assert [r.row_id for r in rows] == ids
        assert (
            await admin.fetchval(
                "SELECT published_at FROM tenant_event_outbox WHERE id::text = $1",
                ids[0],
            )
            is None
        )


async def test_limit_bounds_the_batch(port: int) -> None:
    async with identity_tables(port) as (admin, pool):
        ids = await _seed(admin, 4)
        rows = await TenantOutboxPostgres(pool=pool).fetch_unpublished(2)
        assert [r.row_id for r in rows] == ids[:2]


async def test_a_table_with_the_tracking_columns_counts_attempts_and_keeps_the_error(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        await admin.execute(
            "ALTER TABLE tenant_event_outbox ADD COLUMN attempts integer NOT NULL DEFAULT 0, "
            "ADD COLUMN last_error text"
        )
        ids = await _seed(admin, 1)
        store = TenantOutboxPostgres(pool=pool)
        await store.record_failure(ids[0], "KafkaTimeoutError: x")
        rows = await store.fetch_unpublished(10)
        assert [(r.row_id, r.attempts) for r in rows] == [(ids[0], 1)]
        await store.mark_published(ids[0], NOW)
        stored = await admin.fetchrow(
            "SELECT published_at, last_error FROM tenant_event_outbox WHERE id::text = $1",
            ids[0],
        )
        assert stored is not None
        assert (stored["published_at"], stored["last_error"]) == (NOW, None)


class _Bus:
    """A broker double: records what it acknowledged, or refuses every publish."""

    def __init__(self, refuse: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self._refuse = refuse

    async def publish(self, topic: str, key: str, envelope: dict[str, object]) -> None:
        if self._refuse:
            raise TimeoutError("broker refused")
        self.sent.append((topic, str(envelope["correlation_id"])))


async def _flush(pool: asyncpg.Pool, bus: _Bus) -> ModelTenantOutboxFlushResult:
    handler = HandlerTenantOutboxFlush(
        TenantOutboxPostgres(pool=pool), bus, topic="onex.tenant.events"
    )
    return await handler.handle(
        ModelTenantOutboxFlushCommand(command_id=uuid4(), batch_limit=10)
    )


async def test_an_event_is_published_once_marked_and_a_second_run_sends_nothing(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        (row_id,) = await _seed(admin, 1)
        first_bus, second_bus = _Bus(), _Bus()
        first = await _flush(pool, first_bus)
        second = await _flush(pool, second_bus)
        assert first.outcome is EnumTenantOutboxFlushOutcome.FLUSHED
        assert first_bus.sent == [("onex.tenant.events", "c0")]
        assert (
            await admin.fetchval(
                "SELECT published_at IS NOT NULL FROM tenant_event_outbox WHERE id::text = $1",
                row_id,
            )
            is True
        )
        assert second.outcome is EnumTenantOutboxFlushOutcome.NOTHING_TO_FLUSH
        assert second_bus.sent == []


async def test_a_broker_refusal_keeps_the_row_for_the_next_run(port: int) -> None:
    async with identity_tables(port) as (admin, pool):
        (row_id,) = await _seed(admin, 1)
        refused = await _flush(pool, _Bus(refuse=True))
        assert refused.outcome is EnumTenantOutboxFlushOutcome.PARTIAL
        assert refused.failed == 1
        assert (
            await admin.fetchval(
                "SELECT published_at IS NULL FROM tenant_event_outbox WHERE id::text = $1",
                row_id,
            )
            is True
        )
        retry_bus = _Bus()
        retried = await _flush(pool, retry_bus)
        assert retried.outcome is EnumTenantOutboxFlushOutcome.FLUSHED
        assert retry_bus.sent == [("onex.tenant.events", "c0")]


async def test_a_real_table_without_published_at_is_refused_naming_the_column(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        await _seed(admin, 1)
        await admin.execute("ALTER TABLE tenant_event_outbox DROP COLUMN published_at")
        result = await _flush(pool, _Bus())
        assert result.outcome is EnumTenantOutboxFlushOutcome.FAILED_STORE_UNAVAILABLE
        assert "published_at" in result.detail
