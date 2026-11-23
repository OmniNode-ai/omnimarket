# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof of the terminal writer's single transaction (OMN-26464).

The fake store cannot prove the lock, the status guard or the concurrent-outcome rule: they live
in SQL. The harness mirrors tests/test_omn19716_topic_activity_real_postgres.py. It creates its own
throwaway database with the gateway table's shape, so it never touches a shared schema.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.store_postgres import (
    GatewayTerminalPostgresPool,
    PostgresGatewayTerminalStore,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalStoreOutcome as Store,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_terminal_update import (
    ModelTerminalUpdate,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.store_postgres import (
    PostgresWorkflowStore,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_postgres_pool import (
    WorkflowPostgresPool,
)

_DDL = """
CREATE TABLE public.gateway_workflows (
    workflow_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, principal_id text NOT NULL DEFAULT 'p',
    authenticated_user_id text NOT NULL DEFAULT 'u', workflow_type text NOT NULL DEFAULT 't',
    contract_id text NOT NULL DEFAULT 'c', command_topic text NOT NULL DEFAULT 'x',
    envelope_id uuid NOT NULL DEFAULT gen_random_uuid(), correlation_id uuid NOT NULL,
    causation_id uuid NOT NULL DEFAULT gen_random_uuid(),
    status text NOT NULL CHECK (status IN ('accepted','published','failed_publish','completed','failed')),
    submitted_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    terminal_model_used text, terminal_total_tokens integer, terminal_latency_ms integer,
    completed_at timestamptz, result_content text, terminal_failure_class text,
    terminal_failure_code text, terminal_failure_reason text, terminal_remediation text,
    terminal_route text, terminal_provider text, terminal_credential_source text,
    terminal_rule_evaluations jsonb
);
CREATE INDEX idx_gateway_workflows_correlation ON public.gateway_workflows (correlation_id);
"""


class _FixedSecretStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def get_secret(self, name: str) -> str:
        return self._dsn


def _admin_dsn(database: str) -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping terminal writer DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{database}"


@pytest.fixture
async def database() -> AsyncIterator[str]:
    name = f"gwterm_26464_{uuid4().hex[:12]}"
    try:
        admin = await asyncpg.connect(
            _admin_dsn(os.environ.get("INTEGRATION_POSTGRES_DB", "postgres"))
        )
    except (
        OSError,
        asyncpg.PostgresError,
    ) as exc:
        pytest.skip(f"no reachable Postgres for terminal writer proof: {exc}")
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        dsn = _admin_dsn(name)
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_DDL)
        finally:
            await conn.close()
        yield dsn
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


def _pool(dsn: str) -> GatewayTerminalPostgresPool:
    return GatewayTerminalPostgresPool(
        secret_resolver_config_path="unused",
        dsn_secret="unused",
        max_size=4,
        query_timeout_seconds=10.0,
        secret_store=cast(ProtocolSecretStore, _FixedSecretStore(dsn)),
    )


async def _insert(
    dsn: str, tenant: UUID, correlation: UUID, status: str = "published"
) -> UUID:
    workflow_id = uuid4()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO public.gateway_workflows (workflow_id, tenant_id, correlation_id, status) "
            "VALUES ($1, $2, $3, $4)",
            workflow_id,
            tenant,
            correlation,
            status,
        )
    finally:
        await conn.close()
    return workflow_id


async def _row(dsn: str, workflow_id: UUID) -> dict[str, Any]:
    conn = await asyncpg.connect(dsn)
    try:
        record = await conn.fetchrow(
            "SELECT * FROM public.gateway_workflows WHERE workflow_id = $1", workflow_id
        )
    finally:
        await conn.close()
    assert record is not None
    return dict(record)


def _update(
    tenant: UUID,
    correlation: UUID,
    status: str = "completed",
    model: str = "glm-5.3-flash",
) -> ModelTerminalUpdate:
    return ModelTerminalUpdate(
        tenant_id=tenant,
        correlation_id=correlation,
        status=status,
        model_used=model,
        total_tokens=298,
        latency_ms=7389,
        route="byok-glm-general",
        provider="glm",
        credential_source="customer_key",
        content="answer",
        failure_reason="why" if status == "failed" else None,
        failure_class="SomeError" if status == "failed" else None,
        failure_code="CODE_X" if status == "failed" else None,
        rule_evaluations=[{"rule": "no_refusal", "passed": True}],
    )


@pytest.mark.integration
async def test_real_postgres_writes_every_terminal_column(database: str) -> None:
    tenant, corr = uuid4(), uuid4()
    workflow_id = await _insert(database, tenant, corr)
    store = PostgresGatewayTerminalStore(_pool(database))
    assert await store.apply_terminal(_update(tenant, corr)) == (
        Store.APPLIED,
        workflow_id,
    )
    row = await _row(database, workflow_id)
    assert row["status"] == "completed"
    assert (
        row["terminal_model_used"],
        row["terminal_total_tokens"],
        row["terminal_latency_ms"],
    ) == (
        "glm-5.3-flash",
        298,
        7389,
    )
    assert (row["terminal_route"], row["terminal_provider"]) == (
        "byok-glm-general",
        "glm",
    )
    assert row["terminal_credential_source"] == "customer_key"
    assert row["result_content"] == "answer"
    assert row["completed_at"] is not None
    assert row["terminal_remediation"] is None
    assert row["terminal_rule_evaluations"] is not None


@pytest.mark.integration
@pytest.mark.parametrize("start", ["accepted", "published", "failed_publish"])
async def test_real_postgres_every_writable_starting_status(
    database: str, start: str
) -> None:
    tenant, corr = uuid4(), uuid4()
    workflow_id = await _insert(database, tenant, corr, start)
    outcome = await PostgresGatewayTerminalStore(_pool(database)).apply_terminal(
        _update(tenant, corr, "failed")
    )
    assert outcome == (Store.APPLIED, workflow_id)
    assert (await _row(database, workflow_id))["status"] == "failed"


@pytest.mark.integration
async def test_real_postgres_concurrent_opposite_outcomes_exactly_one_wins(
    database: str,
) -> None:
    tenant, corr = uuid4(), uuid4()
    workflow_id = await _insert(database, tenant, corr)
    store = PostgresGatewayTerminalStore(_pool(database))
    first, second = await asyncio.gather(
        store.apply_terminal(_update(tenant, corr, "completed", "model-a")),
        store.apply_terminal(_update(tenant, corr, "failed", "model-b")),
    )
    outcomes = sorted(result[0].value for result in (first, second))
    assert outcomes == [Store.ALREADY_TERMINAL.value, Store.APPLIED.value]
    row = await _row(database, workflow_id)
    winner = "completed" if first[0] is Store.APPLIED else "failed"
    assert row["status"] == winner
    assert row["terminal_model_used"] == (
        "model-a" if winner == "completed" else "model-b"
    )


@pytest.mark.integration
async def test_real_postgres_tenant_isolation(database: str) -> None:
    mine, other, corr = uuid4(), uuid4(), uuid4()
    mine_id = await _insert(database, mine, corr)
    other_id = await _insert(database, other, corr)
    store = PostgresGatewayTerminalStore(_pool(database))
    assert await store.apply_terminal(_update(mine, corr)) == (Store.APPLIED, mine_id)
    assert (await _row(database, other_id))["status"] == "published"
    assert (await store.apply_terminal(_update(uuid4(), corr)))[0] is Store.NO_ROW


@pytest.mark.integration
async def test_real_postgres_replay_preserves_timestamps(database: str) -> None:
    tenant, corr = uuid4(), uuid4()
    workflow_id = await _insert(database, tenant, corr)
    store = PostgresGatewayTerminalStore(_pool(database))
    await store.apply_terminal(_update(tenant, corr))
    before = await _row(database, workflow_id)
    assert (await store.apply_terminal(_update(tenant, corr, "failed", "late")))[
        0
    ] is Store.ALREADY_TERMINAL
    after = await _row(database, workflow_id)
    assert (after["completed_at"], after["updated_at"], after["status"]) == (
        before["completed_at"],
        before["updated_at"],
        before["status"],
    )


@pytest.mark.integration
async def test_real_postgres_duplicate_rows_are_ambiguous_and_untouched(
    database: str,
) -> None:
    tenant, corr = uuid4(), uuid4()
    ids = [
        await _insert(database, tenant, corr),
        await _insert(database, tenant, corr, "completed"),
    ]
    outcome = await PostgresGatewayTerminalStore(_pool(database)).apply_terminal(
        _update(tenant, corr)
    )
    assert outcome == (Store.AMBIGUOUS, None)
    assert [(await _row(database, i))["status"] for i in ids] == [
        "published",
        "completed",
    ]


@pytest.mark.integration
async def test_real_postgres_submit_published_does_not_overwrite_terminal(
    database: str,
) -> None:
    tenant, corr = uuid4(), uuid4()
    workflow_id = await _insert(database, tenant, corr)
    await PostgresGatewayTerminalStore(_pool(database)).apply_terminal(
        _update(tenant, corr)
    )
    gateway_pool = WorkflowPostgresPool(
        secret_resolver_config_path="unused",
        dsn_secret="unused",
        max_size=2,
        query_timeout_seconds=10.0,
        secret_store=cast(ProtocolSecretStore, _FixedSecretStore(database)),
    )
    gateway_store = PostgresWorkflowStore(
        pool=gateway_pool,
        metric_key="m",
        window_key="w",
        lock_namespace="n",
        rpm_metric_key="rm",
        rpm_window_key="rw",
        clock=lambda: datetime.now(UTC),
    )
    await gateway_store.set_status(
        workflow_id=workflow_id, tenant_id=tenant, status="published"
    )
    assert (await _row(database, workflow_id))["status"] == "completed"
    # A non-terminal row is still moved: the guard narrows nothing else.
    other = await _insert(database, tenant, uuid4(), "accepted")
    await gateway_store.set_status(
        workflow_id=other, tenant_id=tenant, status="published"
    )
    assert (await _row(database, other))["status"] == "published"
