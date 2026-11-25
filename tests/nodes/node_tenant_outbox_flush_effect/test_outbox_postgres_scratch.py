# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres store of the outbox flush against a scratch PostgreSQL 16 holding the dev ``tenant_event_outbox`` shape.

The table is the one ``node_tenant_provision_effect`` writes (id, tenant_id, envelope, created_at, published_at), created
by the provision node's scratch DDL. A recording pool cannot tell whether the store's statements name columns the table has.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.tenant_outbox_postgres import (
    TenantOutboxPostgres,
)
from tests.nodes.node_tenant_provision_effect.conftest import port  # noqa: F401
from tests.nodes.node_tenant_provision_effect.postgres_support import identity_tables

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 11, 25, 7, 0, tzinfo=UTC)


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


async def test_fetch_returns_unpublished_rows_oldest_first(port: int) -> None:  # noqa: F811
    async with identity_tables(port) as (admin, pool):
        ids = await _seed(admin, 3)
        rows = await TenantOutboxPostgres(pool=pool).fetch_unpublished(10)
        assert [r.row_id for r in rows] == ids
        assert rows[0].envelope["correlation_id"] == "c0"


async def test_a_marked_row_is_not_fetched_again(port: int) -> None:  # noqa: F811
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
    port: int,  # noqa: F811
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


async def test_limit_bounds_the_batch(port: int) -> None:  # noqa: F811
    async with identity_tables(port) as (admin, pool):
        ids = await _seed(admin, 4)
        rows = await TenantOutboxPostgres(pool=pool).fetch_unpublished(2)
        assert [r.row_id for r in rows] == ids[:2]


async def test_a_table_with_the_tracking_columns_counts_attempts_and_keeps_the_error(
    port: int,  # noqa: F811
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
