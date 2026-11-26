# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26474 revoke: revoked, already_revoked and not_found, scoped by tenant, on a scratch Postgres."""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest

from omnimarket.nodes.node_tenant_api_key_effect.models import (
    EnumTenantApiKeyRevokeOutcome as Outcome,
)
from tests.nodes.node_tenant_api_key_effect.support import (
    digest,
    revoke_command,
    revoke_handler,
)
from tests.nodes.scratch_identity_postgres import (
    add_key,
    add_tenant,
    scratch_identity_database,
    snapshot,
)

pytestmark = pytest.mark.unit


async def test_revoke_deactivates_the_row_and_a_second_revoke_answers_already_revoked() -> (
    None
):
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        credential = await add_key(dsn, tenant, name="laptop", key_hash=digest("a"))
        handler = revoke_handler(dsn)
        try:
            first = await handler.handle(revoke_command(tenant, credential))
            second = await handler.handle(revoke_command(tenant, credential))
        finally:
            await handler.close()
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow(
                "SELECT is_active, revoked_at FROM tenant_api_keys"
            )
        finally:
            await conn.close()
    assert first.outcome is Outcome.REVOKED
    assert second.outcome is Outcome.ALREADY_REVOKED
    assert row is not None
    assert row["is_active"] is False
    assert row["revoked_at"] is not None


async def test_revoke_a_missing_key_is_not_found() -> None:
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = revoke_handler(dsn)
        try:
            result = await handler.handle(revoke_command(tenant, UUID(int=99)))
        finally:
            await handler.close()
    assert result.outcome is Outcome.NOT_FOUND


async def test_revoke_a_key_of_another_tenant_is_not_found_and_changes_no_row() -> None:
    async with scratch_identity_database() as dsn:
        owner = await add_tenant(dsn)
        stranger = await add_tenant(dsn)
        credential = await add_key(dsn, owner, name="laptop", key_hash=digest("a"))
        before = await snapshot(dsn)
        handler = revoke_handler(dsn)
        try:
            from_stranger = await handler.handle(revoke_command(stranger, credential))
            unchanged = before == await snapshot(dsn)
            by_owner = await handler.handle(revoke_command(owner, credential))
            from_stranger_again = await handler.handle(
                revoke_command(stranger, credential)
            )
        finally:
            await handler.close()
    assert from_stranger.outcome is Outcome.NOT_FOUND
    assert unchanged
    assert by_owner.outcome is Outcome.REVOKED
    # a revoked key of another tenant is still not_found, never already_revoked
    assert from_stranger_again.outcome is Outcome.NOT_FOUND


async def test_revoke_answers_store_unavailable_when_the_database_is_unreachable() -> (
    None
):
    handler = revoke_handler("postgresql://nobody@127.0.0.1:1/none")
    try:
        result = await handler.handle(revoke_command(UUID(int=1), UUID(int=2)))
    finally:
        await handler.close()
    assert result.outcome is Outcome.STORE_UNAVAILABLE
