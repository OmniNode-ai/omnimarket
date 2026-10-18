# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reported decimal money survives the deployed writer's NUMERIC binding.

Failures: a JSON float expands to its binary value in NUMERIC; fixing cash
alone leaves another money column inconsistent; or a sparse write rewrites
an existing measured cost. Real migrated delegation_events, owned Postgres,
and the actual writer are used. Only snapshot network transport is replaced.
The integration_postgres_dsn fixture reads the INTEGRATION_POSTGRES host,
port, user and database settings from tests/conftest.py.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    ModelTaskDelegatedEvent,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)

_TENANT = "00000000-0000-4000-8000-000000021015"
_MONEY_COLUMNS = ("cost_usd", "cost_savings_usd", "budget_headroom_consumed_usd")


@pytest.mark.integration
async def test_the_routed_sync_adapter_stores_the_reported_cent_exactly(
    integration_postgres_dsn: str,
) -> None:
    async with _provisioned_runner(integration_postgres_dsn) as (_, conn, schema):
        adapter = PostgresSyncProjectionAdapter(integration_postgres_dsn, schema=schema)
        correlation_id = str(uuid4())
        returned = await asyncio.to_thread(
            adapter.upsert_returning,
            "delegation_events",
            "correlation_id",
            {
                "correlation_id": correlation_id,
                "tenant_id": _TENANT,
                "task_type": "document",
                "delegated_to": "fixture-serving-model",
                "cost_usd": 0.01,
            },
            returning=("correlation_id", "cost_usd"),
        )
        stored = await conn.fetchval(
            "SELECT cost_usd FROM delegation_events WHERE correlation_id=$1",
            correlation_id,
        )
        assert stored == Decimal("0.01")
        assert returned == [{"correlation_id": correlation_id, "cost_usd": stored}]


class _NoNetworkProducer:
    async def send_and_wait(
        self,
        topic: str,
        *,
        value: bytes | None,
        key: bytes,
        headers: list[tuple[str, bytes]],
    ) -> None:
        return None


@pytest.mark.integration
async def test_a_terminal_stores_the_reported_cent_exactly(
    integration_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _provisioned_runner(integration_postgres_dsn) as (runner, conn, _):
        await conn.execute(
            "CREATE TABLE tenant_registry_mirror "
            "(tenant_slug text PRIMARY KEY, tenant_uuid uuid UNIQUE NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO tenant_registry_mirror VALUES ('decimal-tenant', $1::uuid)",
            UUID(_TENANT),
        )

        async def producer() -> _NoNetworkProducer:
            return _NoNetworkProducer()

        monkeypatch.setattr(runner, "_ensure_producer", producer)
        event = ModelTaskDelegatedEvent.model_validate(
            {
                "correlation_id": str(uuid4()),
                "tenant_id": "decimal-tenant",
                "task_type": "document",
                "delegated_to": "fixture-serving-model",
                "model_name": "fixture-serving-model",
                "delegated_by": "test",
                "quality_gate_passed": True,
                "timestamp": "2026-10-13T12:00:00+00:00",
                "tokens_input": 1000,
                "tokens_output": 200,
                "cost_usd": 0.01,
            }
        )
        payload = event.model_dump(mode="json")
        assert await runner.project_event(
            runner._topic_delegated,
            payload,
            MessageMeta(partition=0, offset=0, fallback_id=str(event.correlation_id)),
        )
        stored = await conn.fetchval(
            "SELECT cost_usd FROM delegation_events WHERE correlation_id=$1",
            str(event.correlation_id),
        )
        assert stored == Decimal("0.01")


@pytest.mark.parametrize("column", _MONEY_COLUMNS)
@pytest.mark.integration
async def test_each_scalar_money_column_binds_the_reported_decimal(
    integration_postgres_dsn: str, column: str
) -> None:
    async with _provisioned_runner(integration_postgres_dsn) as (runner, conn, schema):
        money = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='delegation_events' "
            "AND data_type='numeric' AND column_name LIKE '%usd'",
            schema,
        )
        assert {row["column_name"] for row in money} == set(_MONEY_COLUMNS)
        correlation_id = str(uuid4())
        await runner._dynamic_upsert(
            table=runner._table_delegation,
            conflict_key="correlation_id",
            row={
                "correlation_id": correlation_id,
                "tenant_id": _TENANT,
                "created_at": datetime(2026, 10, 13, tzinfo=UTC),
                "task_type": "document",
                "delegated_to": "fixture-serving-model",
                column: 0.01,
            },
        )
        stored = await conn.fetchrow(
            "SELECT cost_usd, cost_savings_usd, budget_headroom_consumed_usd "
            "FROM delegation_events WHERE correlation_id=$1",
            correlation_id,
        )
        assert stored is not None
        assert stored[column] == Decimal("0.01")


@pytest.mark.integration
async def test_a_sparse_write_does_not_reprice_an_existing_numeric_cost(
    integration_postgres_dsn: str,
) -> None:
    async with _provisioned_runner(integration_postgres_dsn) as (runner, conn, _):
        correlation_id = str(uuid4())
        previous = Decimal("0.010000000000000000208166817117")
        await runner._dynamic_upsert(
            table=runner._table_delegation,
            conflict_key="correlation_id",
            row={
                "correlation_id": correlation_id,
                "tenant_id": _TENANT,
                "task_type": "document",
                "delegated_to": "fixture-serving-model",
                "cost_usd": previous,
            },
        )
        await runner._dynamic_upsert(
            table=runner._table_delegation,
            conflict_key="correlation_id",
            row={"correlation_id": correlation_id, "tenant_id": _TENANT},
        )
        stored = await conn.fetchval(
            "SELECT cost_usd FROM delegation_events WHERE correlation_id=$1",
            correlation_id,
        )
        assert stored == previous


@pytest.mark.integration
async def test_a_nullable_cost_keeps_an_explicit_unpriced_null(
    integration_postgres_dsn: str,
) -> None:
    async with _provisioned_runner(integration_postgres_dsn) as (runner, conn, _):
        # Model the OMN-20717 nullable-cost ruling in this owned scratch schema.
        # This proves parameter binding, not delivery of its product migration.
        await conn.execute(
            "ALTER TABLE delegation_events ALTER COLUMN cost_usd DROP NOT NULL"
        )
        correlation_id = str(uuid4())
        await runner._dynamic_upsert(
            table=runner._table_delegation,
            conflict_key="correlation_id",
            row={
                "correlation_id": correlation_id,
                "tenant_id": _TENANT,
                "task_type": "document",
                "delegated_to": "fixture-serving-model",
                "cost_usd": None,
            },
        )
        stored = await conn.fetchrow(
            "SELECT cost_usd FROM delegation_events WHERE correlation_id=$1",
            correlation_id,
        )
        assert stored is not None
        assert stored["cost_usd"] is None
