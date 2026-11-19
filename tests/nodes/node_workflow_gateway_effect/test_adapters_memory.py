# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory profile adapters: token bucket, store quota semantics and the advisory lock key.

Exemplar: tests/nodes/node_plan_list/fakes.py (seeded in-memory profile binding).
"""

from __future__ import annotations

import hashlib
from uuid import UUID

import pytest

from omnimarket.nodes.node_workflow_gateway_effect.handlers.adapters_memory import (
    InMemoryWorkflowPublisher,
    InMemoryWorkflowStore,
    InProcessTokenBucketLimiter,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_advisory_key import (
    advisory_lock_key,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_envelope import (
    ModelWorkflowCommandEnvelope,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowCaller,
)
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_publisher import (
    WorkflowPublishError,
)
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_store import (
    WorkflowStoreUnavailableError,
)
from tests.nodes.node_workflow_gateway_effect.fakes import (
    NOW,
    SKILL_TOPIC,
    TENANT_A,
    TENANT_B,
)

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_bucket_allows_the_burst_then_denies_with_a_retry_hint() -> None:
    clock = Clock()
    limiter = InProcessTokenBucketLimiter(clock)
    decisions = [
        await limiter.acquire("p", refill_per_minute=60, capacity=2) for _ in range(3)
    ]
    assert [d.allowed for d in decisions] == [True, True, False]
    assert decisions[2].retry_after_seconds >= 1
    assert decisions[2].capacity == 2


async def test_bucket_refills_over_time_and_principals_do_not_share() -> None:
    clock = Clock()
    limiter = InProcessTokenBucketLimiter(clock)
    assert (await limiter.acquire("p", refill_per_minute=60, capacity=1)).allowed
    assert not (await limiter.acquire("p", refill_per_minute=60, capacity=1)).allowed
    assert (await limiter.acquire("other", refill_per_minute=60, capacity=1)).allowed
    clock.now += 1.0  # one token at 60 per minute
    assert (await limiter.acquire("p", refill_per_minute=60, capacity=1)).allowed


def _caller(tenant: UUID) -> ModelWorkflowCaller:
    return ModelWorkflowCaller(
        tenant_id=tenant, tenant_slug="s", principal_id="t-x", authenticated_user_id="k"
    )


def _envelope(n: int) -> ModelWorkflowCommandEnvelope:
    return ModelWorkflowCommandEnvelope(
        envelope_id=UUID(int=n),
        correlation_id=UUID(int=n),
        causation_id=UUID(int=n),
        source_tenant_id=TENANT_A,
        authenticated_user_id="k",
        schema_version="1.1.0",
        emitted_at=NOW,
        source_gateway_instance="g",
        source_tenant_principal_id="t-x",
        workflow_id=UUID(int=n),
        workflow_type="delegate-skill",
        contract_id="c:1",
        command_topic=SKILL_TOPIC,
        event_type="e",
        payload={},
    )


async def test_store_counts_the_tenants_month_and_applies_the_cap() -> None:
    store = InMemoryWorkflowStore(monthly_limit=2, hard_cap=True, clock=lambda: NOW)
    results = [
        await store.admit(caller=_caller(TENANT_A), envelope=_envelope(n))
        for n in (1, 2, 3)
    ]
    assert [r.admitted for r in results] == [True, True, False]
    assert results[2].quota.status == "hard_limit_hit"
    assert (results[2].quota.used, results[2].quota.remaining) == (2, 0)
    # The other tenant's count is separate.
    other = await store.admit(caller=_caller(TENANT_B), envelope=_envelope(4))
    assert other.admitted
    assert await store.fetch(workflow_id=UUID(int=3), tenant_id=TENANT_A) is None


async def test_store_without_a_limit_admits_and_failures_are_typed() -> None:
    store = InMemoryWorkflowStore(monthly_limit=None, hard_cap=True, clock=lambda: NOW)
    assert (
        await store.admit(caller=_caller(TENANT_A), envelope=_envelope(1))
    ).quota.status == "ok"
    broken = InMemoryWorkflowStore(
        monthly_limit=None,
        hard_cap=False,
        clock=lambda: NOW,
        fail_with=RuntimeError("x"),
    )
    with pytest.raises(WorkflowStoreUnavailableError):
        await broken.fetch(workflow_id=UUID(int=1), tenant_id=TENANT_A)


async def test_publisher_records_and_refuses_on_failure() -> None:
    ok = InMemoryWorkflowPublisher()
    await ok.publish(topic="t", key="k", wire={"a": 1})
    assert ok.published == [("t", "k", {"a": 1})]
    failing = InMemoryWorkflowPublisher(fail_with=RuntimeError("x"))
    with pytest.raises(WorkflowPublishError):
        await failing.publish(topic="t", key="k", wire={})
    assert failing.published == []


def test_advisory_lock_key_is_the_hosted_gateways_literal_value() -> None:
    expected = int.from_bytes(hashlib.sha256(b"ns:val").digest()[:8], "big") % 2**63
    assert advisory_lock_key("ns", "val") == expected
    assert 0 <= expected < 2**63
    assert advisory_lock_key("ns", "other") != expected
