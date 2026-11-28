# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26475 suspend: sets provisioning_state=suspended, refuses a tenant with a live gateway or provider key, and is idempotent.

Runs against a scratch PostgreSQL 16 holding the identity tables, through the handler and the store the runtime builds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_provision import (
    HandlerTenantProvision,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_suspend import (
    HandlerTenantSuspend,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantIdentityPostgres,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    EnumTenantSuspendOutcome,
    ModelTenantProvisionCommand,
    ModelTenantSuspendCommand,
)

from .postgres_support import identity_tables

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OUTCOME = EnumTenantSuspendOutcome


def _clock() -> datetime:
    return datetime(2026, 11, 25, 21, 0, 0, tzinfo=UTC)


async def _tenant(pool: asyncpg.Pool, slug: str = "acme-labs") -> UUID:
    created = await HandlerTenantProvision(
        store=TenantIdentityPostgres(pool=pool), clock=_clock
    ).handle(ModelTenantProvisionCommand(command_id=uuid4(), tenant_slug=slug))
    assert created.tenant_id is not None
    return created.tenant_id


def _handler(pool: asyncpg.Pool) -> HandlerTenantSuspend:
    return HandlerTenantSuspend(store=TenantIdentityPostgres(pool=pool), clock=_clock)


def _by_id(tenant_id: UUID) -> ModelTenantSuspendCommand:
    return ModelTenantSuspendCommand(command_id=uuid4(), tenant_id=tenant_id)


async def _state(admin: asyncpg.Connection, tenant_id: UUID) -> str:
    state: str = await admin.fetchval(
        "SELECT provisioning_state FROM tenants WHERE tenant_id = $1", tenant_id
    )
    return state


async def test_a_tenant_with_no_keys_is_suspended_and_a_second_suspend_answers_already_suspended(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        tenant_id = await _tenant(pool)
        handler = _handler(pool)
        first = await handler.handle(_by_id(tenant_id))
        second = await handler.handle(
            ModelTenantSuspendCommand(command_id=uuid4(), tenant_slug="acme-labs")
        )
        state = await _state(admin, tenant_id)
    assert first.outcome is OUTCOME.SUSPENDED
    assert first.provisioning_state == "suspended"
    assert second.outcome is OUTCOME.ALREADY_SUSPENDED
    assert second.tenant_id == tenant_id
    assert state == "suspended"


async def test_a_live_gateway_key_refuses_the_suspend_and_writes_nothing(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        tenant_id = await _tenant(pool)
        await admin.execute(
            "INSERT INTO tenant_api_keys (tenant_id, name, key_hash) VALUES ($1, 'laptop', 'h1')",
            tenant_id,
        )
        answer = await _handler(pool).handle(_by_id(tenant_id))
        state = await _state(admin, tenant_id)
    assert answer.outcome is OUTCOME.REFUSED_LIVE_KEYS
    assert answer.live_gateway_keys == 1
    assert answer.live_provider_keys == 0
    assert state == "active"


async def test_a_live_provider_key_refuses_the_suspend_and_a_revoked_one_does_not(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        tenant_id = await _tenant(pool)
        await admin.execute(
            "INSERT INTO tenant_inference_credentials (api_key_ref, tenant_id, name, provider) "
            "VALUES ('ref-1', $1, 'k', 'openrouter')",
            str(tenant_id),
        )
        refused = await _handler(pool).handle(_by_id(tenant_id))
        state_after_refusal = await _state(admin, tenant_id)
        await admin.execute(
            "UPDATE tenant_inference_credentials SET revoked_at = now() WHERE api_key_ref = 'ref-1'"
        )
        suspended = await _handler(pool).handle(_by_id(tenant_id))
    assert refused.outcome is OUTCOME.REFUSED_LIVE_KEYS
    assert refused.live_provider_keys == 1
    assert refused.live_gateway_keys == 0
    assert state_after_refusal == "active"
    assert suspended.outcome is OUTCOME.SUSPENDED


async def test_a_revoked_gateway_key_and_another_tenants_live_key_do_not_block_the_suspend(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        tenant_id = await _tenant(pool)
        other = await _tenant(pool, "other-labs")
        await admin.execute(
            "INSERT INTO tenant_api_keys (tenant_id, name, key_hash, is_active, revoked_at) "
            "VALUES ($1, 'old', 'h-old', false, now())",
            tenant_id,
        )
        await admin.execute(
            "INSERT INTO tenant_api_keys (tenant_id, name, key_hash) VALUES ($1, 'live', 'h-live')",
            other,
        )
        answer = await _handler(pool).handle(_by_id(tenant_id))
        other_state = await _state(admin, other)
    assert answer.outcome is OUTCOME.SUSPENDED
    assert other_state == "active"


async def test_an_unknown_tenant_is_not_found_and_a_bad_selector_is_refused(
    port: int,
) -> None:
    async with identity_tables(port) as (_admin, pool):
        handler = _handler(pool)
        missing = await handler.handle(_by_id(uuid4()))
        neither = await handler.handle(ModelTenantSuspendCommand(command_id=uuid4()))
        both = await handler.handle(
            ModelTenantSuspendCommand(
                command_id=uuid4(), tenant_id=uuid4(), tenant_slug="acme-labs"
            )
        )
    assert missing.outcome is OUTCOME.NOT_FOUND
    assert neither.outcome is OUTCOME.REFUSED_INVALID_REQUEST
    assert both.outcome is OUTCOME.REFUSED_INVALID_REQUEST
