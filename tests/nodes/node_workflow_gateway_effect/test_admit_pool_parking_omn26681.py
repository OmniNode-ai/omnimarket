# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A burst from one tenant must not park the shared connection pool (OMN-26681).

``PostgresWorkflowStore.admit`` takes a pooled connection and only then waits for the
tenant's advisory lock, so N same-tenant submits hold N connections while they wait. The
pool is shared with every other tenant's auth lookup, rate-limit read and status update,
so a burst of pool-size submits from one tenant starves another tenant's request.

The fake pool below is bounded like asyncpg's (a semaphore of connections) and models
``pg_advisory_xact_lock`` as a lock held until the transaction ends.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID

import pytest

from omnimarket.nodes.node_workflow_gateway_effect.handlers.store_postgres import (
    PostgresWorkflowStore,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_advisory_key import (
    advisory_lock_key,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_postgres_pool import (
    WorkflowPostgresPool,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowCaller,
)
from tests.nodes.node_workflow_gateway_effect.fakes import NOW, TENANT_A
from tests.nodes.node_workflow_gateway_effect.test_store_postgres_sql import _envelope

pytestmark = pytest.mark.unit
TENANT_B = UUID(int=0xB)
POOL_SIZE = 2


def _caller(tenant: UUID) -> ModelWorkflowCaller:
    return ModelWorkflowCaller(
        tenant_id=tenant,
        tenant_slug=str(tenant),
        principal_id=f"t-{tenant}",
        authenticated_user_id="key-1",
    )


class BoundedPool:
    """asyncpg-shaped: POOL_SIZE connections, advisory locks held to transaction end."""

    def __init__(self) -> None:
        self.connections = asyncio.Semaphore(POOL_SIZE)
        self.advisory: dict[int, asyncio.Lock] = {}

    def _lock(self, key: int) -> asyncio.Lock:
        return self.advisory.setdefault(key, asyncio.Lock())

    async def _answer(self, sql: str, args: tuple[Any, ...]) -> Any:
        if "FROM tenant_plans" in sql:
            return ("pro",)
        return None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        async with self.connections:
            return await self._answer(sql, args)

    async def execute(self, sql: str, *args: Any) -> str:
        return "OK"

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_Tx]:
        async with self.connections:
            held: list[asyncio.Lock] = []
            try:
                yield _Tx(self, held)
            finally:
                for lock in held:
                    lock.release()


class _Tx:
    def __init__(self, pool: BoundedPool, held: list[asyncio.Lock]) -> None:
        self._pool = pool
        self._held = held

    async def execute(self, sql: str, *args: Any) -> str:
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            lock = self._pool._lock(int(args[0]))
            await lock.acquire()
            self._held.append(lock)
        return "OK"

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return await self._pool._answer(sql, args)


def _store(pool: BoundedPool) -> PostgresWorkflowStore:
    return PostgresWorkflowStore(
        pool=cast(WorkflowPostgresPool, pool),
        metric_key="workflow_executions",
        window_key="month",
        lock_namespace="workflow_month_quota",
        rpm_metric_key="api_requests",
        rpm_window_key="minute",
        clock=lambda: NOW,
    )


async def test_one_tenants_burst_waiting_on_its_lock_leaves_a_connection_for_another() -> (
    None
):
    pool = BoundedPool()
    store = _store(pool)
    # A slow holder of tenant A's lock (another gateway instance, or a slow quota count).
    holder = pool._lock(advisory_lock_key("workflow_month_quota", str(TENANT_A)))
    await holder.acquire()
    burst = [
        asyncio.create_task(store.admit(caller=_caller(TENANT_A), envelope=_envelope()))
        for _ in range(POOL_SIZE * 3)
    ]
    await asyncio.sleep(0.05)  # every burst submit is now waiting on the lock
    try:
        # Tenant B's own admit must still get a connection and finish.
        admission = await asyncio.wait_for(
            store.admit(caller=_caller(TENANT_B), envelope=_envelope()), timeout=1.0
        )
        assert admission.admitted
        # And so must a plain pooled read (the auth lookup of any tenant).
        await asyncio.wait_for(pool.fetchrow("SELECT 1"), timeout=1.0)
    finally:
        holder.release()
        results = await asyncio.gather(*burst)
    assert all(result.admitted for result in results)
