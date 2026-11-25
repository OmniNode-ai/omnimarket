# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A repeated correlation_id on POST /v1/workflows against the real gateway node and real Postgres.

The route is mounted with the real submit handler and the real PostgresWorkflowStore; only the
bus publisher is in memory. The throwaway database mirrors the hosted gateway table's shape, so
no shared schema is touched. Needs INTEGRATION_POSTGRES_PASSWORD (skips without it, like the
terminal writer's proof).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any, cast
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import httpx
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
from omnimarket.nodes.node_workflow_gateway_effect.handlers.adapters_memory import (
    InMemoryWorkflowStore,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from omnimarket.nodes.node_workflow_gateway_effect.handlers.store_postgres import (
    PostgresWorkflowStore,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_postgres_pool import (
    WorkflowPostgresPool,
)
from tests.nodes.node_workflow_gateway_effect.fakes import (
    API_KEY_HEADER,
    KEY_A,
    KEY_B,
    NOW,
    SKILL_PAYLOAD,
    TENANT_A,
    TENANT_B,
    Gateway,
    build_gateway,
)

pytestmark = pytest.mark.integration

_DDL = """
CREATE TABLE public.gateway_workflows (
    workflow_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, principal_id text NOT NULL,
    authenticated_user_id text NOT NULL, workflow_type text NOT NULL,
    contract_id text NOT NULL, command_topic text NOT NULL,
    envelope_id uuid NOT NULL, correlation_id uuid NOT NULL, causation_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN ('accepted','published','failed_publish','completed','failed')),
    submitted_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    terminal_model_used text, terminal_total_tokens integer, terminal_latency_ms integer,
    completed_at timestamptz, result_content text, terminal_failure_class text,
    terminal_failure_code text, terminal_failure_reason text, terminal_remediation text,
    terminal_route text, terminal_provider text, terminal_credential_source text,
    terminal_rule_evaluations jsonb
);
CREATE INDEX idx_gateway_workflows_correlation ON public.gateway_workflows (correlation_id);
CREATE TABLE tenant_plans (tenant_id uuid, plan_code text, is_active boolean, started_at timestamptz);
CREATE TABLE plan_tiers (code text, is_default boolean, created_at timestamptz DEFAULT now());
CREATE TABLE plan_entitlements (plan_code text, metric_key text, window_key text, limit_value int, hard_cap boolean);
CREATE TABLE public.gateway_rate_limit_events (
    event_id uuid, tenant_id uuid, principal_id text, workflow_type text, correlation_id uuid,
    limit_per_minute int, burst_capacity int, retry_after_seconds int
);
INSERT INTO plan_tiers (code, is_default) VALUES ('free', true);
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
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping gateway DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{database}"


@pytest.fixture
async def database() -> AsyncIterator[str]:
    name = f"gwdup_26680_{uuid4().hex[:12]}"
    try:
        admin = await asyncpg.connect(
            _admin_dsn(os.environ.get("INTEGRATION_POSTGRES_DB", "postgres"))
        )
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for gateway proof: {exc}")
        return
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


def _gateway(dsn: str) -> Gateway:
    pool = WorkflowPostgresPool(
        secret_resolver_config_path="unused",
        dsn_secret="unused",
        max_size=12,
        query_timeout_seconds=10.0,
        secret_store=cast(ProtocolSecretStore, _FixedSecretStore(dsn)),
    )
    store = PostgresWorkflowStore(
        pool=pool,
        metric_key="workflow_executions",
        window_key="month",
        lock_namespace="workflow_month_quota",
        rpm_metric_key="api_requests",
        rpm_window_key="minute",
        clock=lambda: NOW,
    )
    return build_gateway(
        store=cast(InMemoryWorkflowStore, store),
        refill_per_minute=100000,
        burst_capacity=100000,
    )


def _client(gateway: Gateway) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(dependencies=gateway.deps))
    return httpx.AsyncClient(transport=transport, base_url="http://runtime")


def _body(corr: UUID, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "workflow_type": "delegate-skill",
        "correlation_id": str(corr),
        "payload": payload or SKILL_PAYLOAD,
    }


async def _post(client: httpx.AsyncClient, key: str, body: dict[str, Any]) -> Any:
    return await client.post("/v1/workflows", json=body, headers={API_KEY_HEADER: key})


async def _rows(dsn: str, corr: UUID) -> list[Any]:
    conn = await asyncpg.connect(dsn)
    try:
        return list(
            await conn.fetch(
                "SELECT workflow_id, tenant_id, status FROM public.gateway_workflows "
                "WHERE correlation_id = $1",
                corr,
            )
        )
    finally:
        await conn.close()


def _terminal_store(dsn: str) -> PostgresGatewayTerminalStore:
    return PostgresGatewayTerminalStore(
        GatewayTerminalPostgresPool(
            secret_resolver_config_path="unused",
            dsn_secret="unused",
            max_size=4,
            query_timeout_seconds=10.0,
            secret_store=cast(ProtocolSecretStore, _FixedSecretStore(dsn)),
        )
    )


def _terminal(tenant: UUID, corr: UUID) -> ModelTerminalUpdate:
    return ModelTerminalUpdate(
        tenant_id=tenant,
        correlation_id=corr,
        status="completed",
        model_used="m",
        total_tokens=1,
        latency_ms=1,
        route="r",
        provider="p",
        credential_source="house",
        content="answer",
        failure_reason=None,
        failure_class=None,
        failure_code=None,
        rule_evaluations=[],
    )


async def test_same_number_same_payload_returns_the_original_job(database: str) -> None:
    gateway, corr = _gateway(database), uuid4()
    async with _client(gateway) as client:
        first = await _post(client, KEY_A, _body(corr))
        second = await _post(client, KEY_A, _body(corr))
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["workflow_id"] == first.json()["workflow_id"]
    assert second.json()["status"] == "published"
    assert len(await _rows(database, corr)) == 1
    assert len(gateway.publisher.published) == 1  # a replay is not published again


async def test_a_replay_reads_the_current_status_and_the_job_reaches_a_final_state(
    database: str,
) -> None:
    gateway, corr = _gateway(database), uuid4()
    async with _client(gateway) as client:
        first = await _post(client, KEY_A, _body(corr))
        await _post(client, KEY_A, _body(corr))
        outcome = await _terminal_store(database).apply_terminal(
            _terminal(TENANT_A, corr)
        )
        assert outcome == (Store.APPLIED, UUID(first.json()["workflow_id"]))
        replay = await _post(client, KEY_A, _body(corr))
    assert replay.json()["workflow_id"] == first.json()["workflow_id"]
    assert replay.json()["status"] == "completed"


async def test_same_number_different_payload_is_a_typed_conflict(database: str) -> None:
    gateway, corr = _gateway(database), uuid4()
    other = {**SKILL_PAYLOAD, "prompt": "something else"}
    async with _client(gateway) as client:
        first = await _post(client, KEY_A, _body(corr))
        clash = await _post(client, KEY_A, _body(corr, other))
    assert clash.status_code == 409
    assert clash.json()["code"] == "correlation_id_conflict"
    assert "something else" not in clash.text
    assert [str(r["workflow_id"]) for r in await _rows(database, corr)] == [
        first.json()["workflow_id"]
    ]


async def test_another_tenant_with_the_same_number_is_independent(
    database: str,
) -> None:
    gateway, corr = _gateway(database), uuid4()
    other = {**SKILL_PAYLOAD, "prompt": "tenant b prompt"}
    async with _client(gateway) as client:
        a = await _post(client, KEY_A, _body(corr))
        b = await _post(client, KEY_B, _body(corr, other))
    assert b.status_code == 202
    assert b.json()["workflow_id"] != a.json()["workflow_id"]
    assert {r["tenant_id"] for r in await _rows(database, corr)} == {TENANT_A, TENANT_B}


async def test_ten_concurrent_repeats_make_one_job(database: str) -> None:
    gateway, corr = _gateway(database), uuid4()
    async with _client(gateway) as client:
        results = await asyncio.gather(
            *(_post(client, KEY_A, _body(corr)) for _ in range(10))
        )
    assert {r.status_code for r in results} == {202}
    assert len({r.json()["workflow_id"] for r in results}) == 1
    assert len(await _rows(database, corr)) == 1
    assert len(gateway.publisher.published) == 1
