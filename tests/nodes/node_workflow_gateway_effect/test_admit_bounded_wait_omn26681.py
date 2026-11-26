# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A same-tenant submit that cannot be admitted in time is refused 429, never left waiting (OMN-26681).

``PostgresWorkflowStore._tenant_gate`` serializes one tenant's admits with no deadline, so a
burst of 60 behind a slow admit waits past the client's 30 s timeout while the server later
logs 202. The wait for the gate is now bounded: past it, ``admit`` raises
``WorkflowAdmitBusyError`` and the submit handler answers 429 with a Retry-After.
"""

from __future__ import annotations

import asyncio
from typing import cast

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
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_store import (
    WorkflowAdmitBusyError,
)
from tests.nodes.node_workflow_gateway_effect.fakes import NOW, TENANT_A
from tests.nodes.node_workflow_gateway_effect.test_admit_pool_parking_omn26681 import (
    TENANT_B,
    BoundedPool,
    _caller,
)
from tests.nodes.node_workflow_gateway_effect.test_store_postgres_sql import _envelope

pytestmark = pytest.mark.unit


def _store(pool: BoundedPool, wait: float) -> PostgresWorkflowStore:
    return PostgresWorkflowStore(
        pool=cast(WorkflowPostgresPool, pool),
        metric_key="workflow_executions",
        window_key="month",
        lock_namespace="workflow_month_quota",
        rpm_metric_key="api_requests",
        rpm_window_key="minute",
        clock=lambda: NOW,
        admit_wait_seconds=wait,
    )


async def test_admit_behind_a_slow_holder_is_refused_busy_within_the_bound() -> None:
    pool = BoundedPool()
    store = _store(pool, wait=0.2)
    holder = pool._lock(advisory_lock_key("workflow_month_quota", str(TENANT_A)))
    await holder.acquire()
    first = asyncio.create_task(
        store.admit(caller=_caller(TENANT_A), envelope=_envelope())
    )
    await asyncio.sleep(0.05)  # first holds the in-process gate, parked on the lock
    try:
        with pytest.raises(WorkflowAdmitBusyError) as busy:
            await asyncio.wait_for(
                store.admit(caller=_caller(TENANT_A), envelope=_envelope()),
                timeout=2.0,
            )
        assert busy.value.retry_after_seconds >= 1
        # Another tenant is not delayed by it.
        other = await asyncio.wait_for(
            store.admit(caller=_caller(TENANT_B), envelope=_envelope()), timeout=1.0
        )
        assert other.admitted
    finally:
        holder.release()
        await first
