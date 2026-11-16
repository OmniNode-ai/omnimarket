# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres store and the API-key identity against a recording pool (no database).

Exemplar: tests/nodes/node_plan_list/fakes.py (a fake behind the declared port). This reads the
order of statements, the lock, the transaction boundary and the month window, which a seeded
in-memory store cannot.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest

from omnimarket.nodes.node_workflow_gateway_effect.handlers.identity_api_key_postgres import (
    PostgresApiKeyTenantIdentity,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.store_postgres import (
    PostgresWorkflowStore,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_advisory_key import (
    advisory_lock_key,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_postgres_pool import (
    WorkflowPostgresPool,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_envelope import (
    ModelWorkflowCommandEnvelope,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowCaller,
)
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_store import (
    WorkflowStoreUnavailableError,
)
from tests.nodes.node_workflow_gateway_effect.fakes import NOW, SKILL_TOPIC, TENANT_A

pytestmark = pytest.mark.unit
CALLER = ModelWorkflowCaller(
    tenant_id=TENANT_A,
    tenant_slug="a",
    principal_id="t-a",
    authenticated_user_id="key-1",
)


class RecordingPool:
    """Answers canned rows by SQL fragment and records every statement and the transaction."""

    def __init__(
        self, answers: dict[str, Any] | None = None, fail: bool = False
    ) -> None:
        self.answers = answers or {}
        self.log: list[tuple[str, tuple[Any, ...]]] = []
        self.events: list[str] = []
        self.fail = fail

    def _answer(self, sql: str, args: tuple[Any, ...]) -> Any:
        if self.fail:
            raise WorkflowStoreUnavailableError("down")
        self.log.append((sql, args))
        return next((v for k, v in self.answers.items() if k in sql), None)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return list(self._answer(sql, args) or [])

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._answer(sql, args)

    async def execute(self, sql: str, *args: Any) -> str:
        self._answer(sql, args)
        return "OK"

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[RecordingPool]:
        self.events.append("begin")
        yield self
        self.events.append("commit")


def _store(pool: RecordingPool) -> PostgresWorkflowStore:
    return PostgresWorkflowStore(
        pool=cast(WorkflowPostgresPool, pool),
        metric_key="workflow_executions",
        window_key="month",
        lock_namespace="workflow_month_quota",
        rpm_metric_key="api_requests",
        rpm_window_key="minute",
        clock=lambda: NOW,
    )


def _envelope() -> ModelWorkflowCommandEnvelope:
    return ModelWorkflowCommandEnvelope(
        envelope_id=UUID(int=1),
        correlation_id=UUID(int=2),
        causation_id=UUID(int=1),
        source_tenant_id=TENANT_A,
        authenticated_user_id="key-1",
        schema_version="1.1.0",
        emitted_at=NOW,
        source_gateway_instance="g",
        source_tenant_principal_id="t-a",
        workflow_id=UUID(int=3),
        workflow_type="delegate-skill",
        contract_id="c:1",
        command_topic=SKILL_TOPIC,
        event_type="e",
        payload={},
    )


PLAN = {"FROM tenant_plans": ("pro",)}


async def test_admit_locks_first_inside_one_transaction_then_reads_then_inserts() -> (
    None
):
    pool = RecordingPool(
        {**PLAN, "FROM plan_entitlements": (10, True), "COUNT(*)": (3,)}
    )
    admission = await _store(pool).admit(caller=CALLER, envelope=_envelope())
    assert admission.admitted
    assert admission.quota.status == "ok"
    assert (
        admission.quota.used,
        admission.quota.limit_value,
        admission.quota.remaining,
    ) == (3, 10, 7)
    assert pool.log[0][0] == "SELECT pg_advisory_xact_lock($1)"
    assert pool.log[0][1] == (advisory_lock_key("workflow_month_quota", str(TENANT_A)),)
    assert pool.log[-1][0].startswith("INSERT INTO public.gateway_workflows")
    assert pool.events == ["begin", "commit"]
    count_args = next(args for sql, args in pool.log if "COUNT(*)" in sql)
    # The window is the clock's UTC calendar month, as datetimes (the driver refuses strings).
    assert count_args == (
        TENANT_A,
        datetime(2026, 11, 1, tzinfo=UTC),
        datetime(2026, 12, 1, tzinfo=UTC),
    )
    assert admission.quota.window_end == "2026-12-01T00:00:00Z"


async def test_a_hard_limit_inserts_nothing() -> None:
    pool = RecordingPool(
        {**PLAN, "FROM plan_entitlements": (3, True), "COUNT(*)": (3,)}
    )
    admission = await _store(pool).admit(caller=CALLER, envelope=_envelope())
    assert not admission.admitted
    assert admission.quota.status == "hard_limit_hit"
    assert not any(sql.startswith("INSERT") for sql, _ in pool.log)


async def test_a_soft_limit_admits_and_no_entitlement_means_no_limit() -> None:
    soft = RecordingPool(
        {**PLAN, "FROM plan_entitlements": (3, False), "COUNT(*)": (3,)}
    )
    admitted = await _store(soft).admit(caller=CALLER, envelope=_envelope())
    assert admitted.admitted
    assert admitted.quota.status == "soft_limit"
    free = RecordingPool(PLAN)
    unlimited = await _store(free).admit(caller=CALLER, envelope=_envelope())
    assert unlimited.admitted
    assert unlimited.quota.limit_value is None
    assert not any("COUNT(*)" in sql for sql, _ in free.log)


async def test_the_default_plan_is_used_when_the_tenant_has_none() -> None:
    pool = RecordingPool({"FROM plan_tiers": ("free",)})
    await _store(pool).admit(caller=CALLER, envelope=_envelope())
    assert any("FROM plan_tiers" in sql for sql, _ in pool.log)


async def test_no_plan_at_all_is_a_store_failure() -> None:
    with pytest.raises(WorkflowStoreUnavailableError):
        await _store(RecordingPool()).admit(caller=CALLER, envelope=_envelope())


async def test_fetch_maps_a_row_and_a_miss_is_none() -> None:
    row = (
        UUID(int=3), TENANT_A, "delegate-skill", "published", UUID(int=1), UUID(int=2),
        "topic", NOW, NOW, None, None, None, None, None, None, None, None, None, None, None, None,
    )  # fmt: skip
    found = await _store(RecordingPool({"FROM public.gateway_workflows": row})).fetch(
        workflow_id=UUID(int=3), tenant_id=TENANT_A
    )
    assert found is not None
    assert found.status == "published"
    assert (
        await _store(RecordingPool()).fetch(workflow_id=UUID(int=3), tenant_id=TENANT_A)
        is None
    )


async def test_rpm_override_reads_the_plan_entitlement() -> None:
    pool = RecordingPool({**PLAN, "SELECT limit_value FROM plan_entitlements": (120,)})
    assert await _store(pool).rpm_override(tenant_id=TENANT_A) == 120
    assert (await _store(RecordingPool(PLAN)).rpm_override(tenant_id=TENANT_A)) is None


async def test_a_pool_failure_surfaces_as_a_store_failure() -> None:
    with pytest.raises(WorkflowStoreUnavailableError):
        await _store(RecordingPool(fail=True)).fetch(
            workflow_id=UUID(int=3), tenant_id=TENANT_A
        )


async def test_api_key_identity_rejects_a_wrong_prefix_without_touching_the_pool() -> (
    None
):
    pool = RecordingPool()
    identity = PostgresApiKeyTenantIdentity(
        pool=cast(WorkflowPostgresPool, pool), header="x-api-key", prefix="onxk_"
    )
    assert await identity.resolve({"x-api-key": "other_abc"}) is None
    assert await identity.resolve({"x-api-key": ""}) is None
    assert await identity.resolve({}) is None
    assert pool.log == []


async def test_api_key_identity_resolves_a_hashed_key_to_its_tenant_and_key_id() -> (
    None
):
    key = "onxk_secret"
    pool = RecordingPool({"FROM tenant_api_keys": (TENANT_A, "tenant-a", UUID(int=9))})
    identity = PostgresApiKeyTenantIdentity(
        pool=cast(WorkflowPostgresPool, pool), header="X-API-Key", prefix="onxk_"
    )
    resolved = await identity.resolve({"x-api-key": key})
    assert resolved is not None
    assert (resolved.tenant_id, resolved.tenant_slug, resolved.principal_id) == (
        TENANT_A,
        "tenant-a",
        str(UUID(int=9)),
    )
    assert pool.log[0][1] == (
        hashlib.sha256(key.encode()).hexdigest(),
    )  # the hash, never the key
    assert not any(key in str(args) for _, args in pool.log)
    missing = PostgresApiKeyTenantIdentity(
        pool=cast(WorkflowPostgresPool, RecordingPool()),
        header="x-api-key",
        prefix="onxk_",
    )
    assert await missing.resolve({"x-api-key": key}) is None
