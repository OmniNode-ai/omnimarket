# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""(b), (c), (d) and the replay and refusal cases against a scratch PostgreSQL 16 holding the dev identity tables."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_provision import (
    HandlerTenantProvision,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_read import (
    HandlerTenantRead,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantIdentityPostgres,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    EnumTenantProvisionOutcome,
    EnumTenantReadOutcome,
    ModelTenantProvisionCommand,
    ModelTenantReadCommand,
)
from omnimarket.projection.tenant_registry_events import ModelTenantRegistryEvent

from .postgres_support import identity_tables

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OUTCOME = EnumTenantProvisionOutcome


def _clock(second: int = 0) -> datetime:
    return datetime(2026, 11, 22, 21, 0, second, tzinfo=UTC)


def _handler(
    pool: asyncpg.Pool, tick: list[int] | None = None
) -> HandlerTenantProvision:
    ticks = tick if tick is not None else [0]

    def clock() -> datetime:
        ticks[0] += 1
        return _clock(ticks[0])

    return HandlerTenantProvision(store=TenantIdentityPostgres(pool=pool), clock=clock)


def _command(
    slug: str = "acme-labs", command_id: UUID | None = None, **owner: str
) -> ModelTenantProvisionCommand:
    return ModelTenantProvisionCommand(
        command_id=command_id or uuid4(), tenant_slug=slug, name="Acme Labs", **owner
    )


async def _counts(admin: asyncpg.Connection) -> tuple[int, int, int]:
    return (
        int(await admin.fetchval("SELECT count(*) FROM tenants")),
        int(await admin.fetchval("SELECT count(*) FROM tenant_plans")),
        int(await admin.fetchval("SELECT count(*) FROM tenant_event_outbox")),
    )


async def test_b_double_run_of_one_slug_gives_one_tenant_one_plan_one_outbox_row(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        handler = _handler(pool)
        first = await handler.handle(_command())
        second = await handler.handle(_command())
        assert (first.outcome, second.outcome) == (
            OUTCOME.CREATED,
            OUTCOME.ALREADY_EXISTS,
        )
        assert await _counts(admin) == (1, 1, 1)
        row = await admin.fetchrow(
            "SELECT provisioning_state, owner_email, created_by_sub FROM tenants"
        )
        assert row is not None
        assert (
            row["provisioning_state"],
            row["owner_email"],
            row["created_by_sub"],
        ) == (
            "active",
            None,
            None,
        )
        plan = await admin.fetchval(
            "SELECT plan_code FROM tenant_plans WHERE is_active"
        )
        assert plan == "discovery"
        envelope = await admin.fetchval(
            "SELECT envelope::text FROM tenant_event_outbox"
        )
        assert (
            await admin.fetchval("SELECT topic FROM tenant_event_outbox")
            == "onex.tenant.events"
        )
        parsed = ModelTenantRegistryEvent.from_envelope(json.loads(envelope))
        assert parsed.operation == "TENANT_CREATED"
        assert parsed.tenant.status == "active"
        assert parsed.tenant.tenant_id == first.tenant_id


async def test_c_a_slug_seeded_with_an_owner_email_is_refused_and_nothing_is_written(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        await admin.execute(
            "INSERT INTO tenants (tenant_slug, slug, owner_email) VALUES ('acme-labs', 'acme-labs', 'o@example.invalid')"
        )
        result = await _handler(pool).handle(_command())
        assert result.outcome == OUTCOME.REFUSED_SLUG_TAKEN
        assert result.tenant_id is None
        assert await _counts(admin) == (1, 0, 0)


async def test_d_owner_fields_are_refused_and_nothing_is_written(port: int) -> None:
    async with identity_tables(port) as (admin, pool):
        for owner in (
            {"owner_email": "o@example.invalid"},
            {"owner_username": "owner"},
            {"realm": "omninode"},
        ):
            result = await _handler(pool).handle(_command(**owner))
            assert result.outcome == OUTCOME.REFUSED_OWNER_NOT_SUPPORTED
        assert await _counts(admin) == (0, 0, 0)


async def test_replay_same_command_answers_the_original_payload_except_emitted_at(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        handler = _handler(pool)
        command = _command()
        first = await handler.handle(command)
        again = await handler.handle(command)
        assert first.outcome == again.outcome == OUTCOME.CREATED
        assert first.emitted_at != again.emitted_at
        assert first.model_dump_json(exclude={"emitted_at"}) == again.model_dump_json(
            exclude={"emitted_at"}
        )
        assert await _counts(admin) == (1, 1, 1)


async def test_replay_same_command_survives_a_new_handler_and_pool(port: int) -> None:
    async with identity_tables(port) as (admin, pool):
        command = _command()
        first = await _handler(pool).handle(command)
        again = await _handler(pool, [40]).handle(command)
        assert first.model_dump_json(exclude={"emitted_at"}) == again.model_dump_json(
            exclude={"emitted_at"}
        )
        assert await _counts(admin) == (1, 1, 1)


async def test_a_command_id_reused_for_another_slug_is_refused_with_no_write(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        handler = _handler(pool)
        command = _command()
        await handler.handle(command)
        reused = await handler.handle(_command("other-tenant", command.command_id))
        assert reused.outcome == OUTCOME.REFUSED_COMMAND_ID_REUSED
        assert await _counts(admin) == (1, 1, 1)


async def test_a_command_id_reused_for_another_owner_is_refused_with_no_write(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        handler = _handler(pool)
        command = _command()
        await handler.handle(command)
        await admin.execute("UPDATE tenants SET owner_email = 'o@example.invalid'")
        again = await handler.handle(command)
        assert again.outcome == OUTCOME.REFUSED_COMMAND_ID_REUSED
        assert await _counts(admin) == (1, 1, 1)


async def test_an_invalid_slug_is_refused_with_no_write(port: int) -> None:
    async with identity_tables(port) as (admin, pool):
        for slug in (
            "ab",
            "Upper-Case",
            "-lead",
            "trail-",
            "has space",
            "x" * 64,
            "admin",
            "www",
        ):
            result = await _handler(pool).handle(_command(slug))
            assert result.outcome == OUTCOME.REFUSED_INVALID_SLUG, slug
        assert await _counts(admin) == (0, 0, 0)


async def test_no_default_plan_rolls_the_whole_transaction_back(port: int) -> None:
    async with identity_tables(port) as (admin, pool):
        await admin.execute("UPDATE plan_tiers SET is_default = false")
        result = await _handler(pool).handle(_command())
        assert result.outcome == OUTCOME.FAILED_NO_DEFAULT_PLAN
        assert await _counts(admin) == (0, 0, 0)


async def test_an_unreachable_store_answers_a_typed_failure(port: int) -> None:
    async with identity_tables(port) as (_admin, pool):
        await pool.close()
        result = await _handler(pool).handle(_command())
        assert result.outcome == OUTCOME.FAILED_STORE_UNAVAILABLE


async def test_read_answers_by_id_and_by_slug_and_not_found(port: int) -> None:
    async with identity_tables(port) as (_admin, pool):
        created = await _handler(pool).handle(_command())
        reader = HandlerTenantRead(
            store=TenantIdentityPostgres(pool=pool), clock=lambda: _clock()
        )
        by_slug = await reader.handle(
            ModelTenantReadCommand(command_id=uuid4(), tenant_slug="acme-labs")
        )
        by_id = await reader.handle(
            ModelTenantReadCommand(command_id=uuid4(), tenant_id=created.tenant_id)
        )
        assert by_slug.outcome == by_id.outcome == EnumTenantReadOutcome.FOUND
        assert (by_slug.tenant_id, by_slug.tenant_slug, by_slug.name) == (
            created.tenant_id,
            "acme-labs",
            "Acme Labs",
        )
        assert (by_slug.provisioning_state, by_slug.plan_code) == (
            "active",
            "discovery",
        )
        assert by_slug.created_at is not None
        missing = await reader.handle(
            ModelTenantReadCommand(command_id=uuid4(), tenant_slug="no-such-tenant")
        )
        assert missing.outcome == EnumTenantReadOutcome.NOT_FOUND
        neither = await reader.handle(ModelTenantReadCommand(command_id=uuid4()))
        assert neither.outcome == EnumTenantReadOutcome.REFUSED_INVALID_REQUEST
        both = await reader.handle(
            ModelTenantReadCommand(
                command_id=uuid4(), tenant_slug="acme-labs", tenant_id=created.tenant_id
            )
        )
        assert both.outcome == EnumTenantReadOutcome.REFUSED_INVALID_REQUEST


async def test_an_existing_tenant_with_no_created_event_gets_one_queued_once(
    port: int,
) -> None:
    """A tenant made outside this node (cli-run-a on dev) has no TENANT_CREATED row, so the mirror never learns it."""
    async with identity_tables(port) as (admin, pool):
        tenant_id = await admin.fetchval(
            "INSERT INTO tenants (tenant_slug, slug, name, provisioning_state) "
            "VALUES ('cli-run-a', 'cli-run-a', 'CLI run A', 'active') RETURNING tenant_id"
        )
        await admin.execute(
            "INSERT INTO tenant_plans (tenant_id, plan_code) VALUES ($1, 'discovery')",
            tenant_id,
        )
        assert await _counts(admin) == (1, 1, 0)
        handler = _handler(pool)
        first = await handler.handle(_command("cli-run-a"))
        second = await handler.handle(_command("cli-run-a"))
        assert (first.outcome, second.outcome) == (
            OUTCOME.ALREADY_EXISTS,
            OUTCOME.ALREADY_EXISTS,
        )
        assert await _counts(admin) == (1, 1, 1)
        row = await admin.fetchrow(
            "SELECT topic, envelope::text AS env FROM tenant_event_outbox"
        )
        assert row is not None
        assert row["topic"] == "onex.tenant.events"
        parsed = ModelTenantRegistryEvent.from_envelope(json.loads(row["env"]))
        assert parsed.operation == "TENANT_CREATED"
        assert parsed.tenant.tenant_id == tenant_id
        assert parsed.tenant.tenant_slug == "cli-run-a"
        assert parsed.tenant.plan_code == "discovery"
