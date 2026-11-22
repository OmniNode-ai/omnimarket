# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26474: the gateway's key resolver refuses an expired key and a key of a tenant that is not active.

Real SQL on a scratch Postgres (tests/nodes/scratch_identity_postgres.py). The absent-column case goes through an HTTP
request: every API-key request answers 503 with the refusal code while a bearer request in the same test still answers 202.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import asyncpg
import httpx
import pytest

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_workflow_gateway_effect.handlers.caller_identity import (
    CompositeCallerIdentity,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from omnimarket.nodes.node_workflow_gateway_effect.handlers.identity_api_key_postgres import (
    PostgresApiKeyTenantIdentity,
    build_api_key_identity,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_postgres_pool import (
    WorkflowPostgresPool,
)
from tests.nodes.node_workflow_gateway_effect.fakes import (
    SKILL_PAYLOAD,
    TENANT_A,
    build_gateway,
)
from tests.nodes.scratch_identity_postgres import (
    add_key,
    add_tenant,
    scratch_identity_database,
)

pytestmark = pytest.mark.unit

HEADER = "x-api-key"
PREFIX = "onxk_"
SUBMISSION = {"workflow_type": "delegate-skill", "payload": SKILL_PAYLOAD}
OUTDATED_CODE = "api_key_store_schema_outdated"


class _DsnStore:
    """The secret store the pool asks for its DSN by logical name."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def get_secret(self, name: str) -> str:
        return self._dsn


def _pool(dsn: str) -> WorkflowPostgresPool:
    return WorkflowPostgresPool(
        secret_resolver_config_path="unused",
        dsn_secret="unused",
        max_size=2,
        query_timeout_seconds=10,
        secret_store=_DsnStore(dsn),  # type: ignore[arg-type]
    )


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _identity(pool: WorkflowPostgresPool) -> PostgresApiKeyTenantIdentity:
    return PostgresApiKeyTenantIdentity(pool=pool, header=HEADER, prefix=PREFIX)


async def test_an_unexpired_and_a_perpetual_key_resolve_their_tenant() -> None:
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        future = datetime.now(UTC) + timedelta(hours=1)
        await add_key(
            dsn, tenant, name="dated", key_hash=_digest("onxk_dated"), expires_at=future
        )
        await add_key(dsn, tenant, name="perpetual", key_hash=_digest("onxk_perpetual"))
        pool = _pool(dsn)
        try:
            identity = _identity(pool)
            dated = await identity.resolve({HEADER: "onxk_dated"})
            perpetual = await identity.resolve({HEADER: "onxk_perpetual"})
        finally:
            await pool.close()
    assert dated is not None
    assert dated.tenant_id == tenant
    assert perpetual is not None
    assert perpetual.tenant_id == tenant


async def test_an_expired_key_resolves_none() -> None:
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        past = datetime.now(UTC) - timedelta(seconds=5)
        await add_key(
            dsn, tenant, name="old", key_hash=_digest("onxk_old"), expires_at=past
        )
        pool = _pool(dsn)
        try:
            assert await _identity(pool).resolve({HEADER: "onxk_old"}) is None
        finally:
            await pool.close()


async def test_a_key_of_a_suspended_tenant_resolves_none() -> None:
    async with scratch_identity_database() as dsn:
        suspended = await add_tenant(dsn, state="suspended")
        active = await add_tenant(dsn)
        await add_key(dsn, suspended, name="held", key_hash=_digest("onxk_held"))
        await add_key(dsn, active, name="live", key_hash=_digest("onxk_live"))
        pool = _pool(dsn)
        try:
            identity = _identity(pool)
            assert await identity.resolve({HEADER: "onxk_held"}) is None
            live = await identity.resolve({HEADER: "onxk_live"})
        finally:
            await pool.close()
    assert live is not None
    assert live.tenant_id == active


async def test_a_revoked_key_resolves_none_on_the_very_next_call() -> None:
    # Regression guard: no cache sits in front of the query (this case passes on the SQL before OMN-26474 too).
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        await add_key(dsn, tenant, name="soon-gone", key_hash=_digest("onxk_soon"))
        pool = _pool(dsn)
        try:
            identity = _identity(pool)
            assert await identity.resolve({HEADER: "onxk_soon"}) is not None
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(
                    "UPDATE tenant_api_keys SET is_active = false, revoked_at = now()"
                )
            finally:
                await conn.close()
            assert await identity.resolve({HEADER: "onxk_soon"}) is None
        finally:
            await pool.close()


class _RecordingPool:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def fetchrow(self, sql: str, *args: object) -> None:
        self.statements.append(sql)

    async def execute(self, sql: str, *args: object) -> str:
        self.statements.append(sql)
        return "OK"


@pytest.mark.parametrize(
    "value", ["onxk_é-key", "onxk_ÿ", "é" * 6, "\uff4f\uff4e\uff58\uff4b_wide"]
)
async def test_a_non_ascii_header_resolves_none_without_querying_postgres(
    value: str,
) -> None:
    pool = _RecordingPool()
    identity = PostgresApiKeyTenantIdentity(
        pool=cast(WorkflowPostgresPool, pool), header=HEADER, prefix=PREFIX
    )
    assert await identity.resolve({HEADER: value}) is None
    assert pool.statements == []


class _BearerFake:
    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        if headers.get("authorization") == "Bearer good":
            return ModelHttpTenantIdentity(
                tenant_id=TENANT_A, tenant_slug="tenant-a", principal_id="bearer-user"
            )
        return None


async def test_an_absent_expires_at_column_answers_every_api_key_request_503_and_leaves_bearer_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with scratch_identity_database(with_expires_at=False) as dsn:
        tenant = await add_tenant(dsn)
        await add_key(dsn, tenant, name="any", key_hash=_digest("onxk_any"))
        pool = _pool(dsn)
        try:
            with caplog.at_level(logging.ERROR):
                api_key = await build_api_key_identity(
                    pool=pool, header=HEADER, prefix=PREFIX
                )
            gateway = build_gateway()
            deps = replace(
                gateway.deps,
                identity=CompositeCallerIdentity(
                    bearer=_BearerFake(), api_key=api_key, api_key_header=HEADER
                ),
            )
            transport = httpx.ASGITransport(app=create_app(dependencies=deps))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://runtime"
            ) as client:
                keyed = await client.post(
                    "/v1/workflows", json=SUBMISSION, headers={HEADER: "onxk_any"}
                )
                unknown = await client.post(
                    "/v1/workflows", json=SUBMISSION, headers={HEADER: "onxk_unknown"}
                )
                bearer = await client.post(
                    "/v1/workflows",
                    json=SUBMISSION,
                    headers={"authorization": "Bearer good"},
                )
        finally:
            await pool.close()
    assert keyed.status_code == 503
    assert keyed.json()["code"] == OUTDATED_CODE
    assert unknown.status_code == 503
    assert unknown.json()["code"] == OUTDATED_CODE
    assert bearer.status_code == 202
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert OUTDATED_CODE in errors[0].getMessage()


async def test_a_present_expires_at_column_binds_the_real_resolver() -> None:
    # Positive control of the probe: the same build on a current schema answers with the resolver, not the 503 variant.
    async with scratch_identity_database() as dsn:
        pool = _pool(dsn)
        try:
            identity = await build_api_key_identity(
                pool=pool, header=HEADER, prefix=PREFIX
            )
        finally:
            await pool.close()
    assert isinstance(identity, PostgresApiKeyTenantIdentity)
