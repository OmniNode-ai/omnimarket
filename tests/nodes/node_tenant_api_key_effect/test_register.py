# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26474 register: dedup, refusals and the two-connection race, on a scratch Postgres."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import asyncpg
import pytest

from omnimarket.nodes.node_tenant_api_key_effect.models import (
    EnumTenantApiKeyRegisterOutcome as Outcome,
)
from omnimarket.nodes.node_tenant_api_key_effect.models import (
    ModelTenantApiKeyRow,
)
from omnimarket.nodes.node_tenant_api_key_effect.protocols import (
    ProtocolTenantApiKeyStore,
)
from tests.nodes.node_tenant_api_key_effect.support import (
    DEV_OVERLAY,
    HOSTED_OVERLAY,
    digest,
    in_hours,
    register_command,
    register_handler,
    revoke_command,
    revoke_handler,
    store_for,
)
from tests.nodes.scratch_identity_postgres import (
    add_key,
    add_tenant,
    scratch_identity_database,
)

pytestmark = pytest.mark.unit


async def _count_keys(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        count: int = await conn.fetchval("SELECT count(*) FROM tenant_api_keys")
        return count
    finally:
        await conn.close()


async def test_register_inserts_the_digest_and_answers_ids_and_times_only() -> None:
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        expires = in_hours(2)
        try:
            result = await handler.handle(register_command(tenant, expires_at=expires))
        finally:
            await handler.close()
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow(
                "SELECT id, tenant_id, name, key_hash, is_active, expires_at FROM tenant_api_keys"
            )
        finally:
            await conn.close()
    assert result.outcome is Outcome.REGISTERED
    assert row is not None
    assert result.credential_id == row["id"]
    assert (row["tenant_id"], row["name"], row["is_active"]) == (tenant, "laptop", True)
    assert row["key_hash"] == digest(f"{tenant}/laptop")
    assert row["expires_at"] == expires
    assert result.created_at is not None


async def test_register_replay_with_the_same_name_and_hash_answers_already_registered_with_the_same_id() -> (
    None
):
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        try:
            first = await handler.handle(register_command(tenant))
            again = await handler.handle(register_command(tenant))
        finally:
            await handler.close()
        assert await _count_keys(dsn) == 1
    assert first.outcome is Outcome.REGISTERED
    assert again.outcome is Outcome.ALREADY_REGISTERED
    assert again.credential_id == first.credential_id


async def test_register_same_name_with_another_hash_is_a_name_conflict() -> None:
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        try:
            await handler.handle(register_command(tenant))
            other = await handler.handle(
                register_command(tenant, key_hash=digest("another"))
            )
        finally:
            await handler.close()
        assert await _count_keys(dsn) == 1
    assert other.outcome is Outcome.REFUSED_NAME_CONFLICT


async def test_register_a_duplicate_hash_is_a_hash_collision_for_another_name_or_tenant() -> (
    None
):
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        other_tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        shared = digest("shared")
        try:
            first = await handler.handle(register_command(tenant, key_hash=shared))
            other_name = await handler.handle(
                register_command(tenant, name="desktop", key_hash=shared)
            )
            other_owner = await handler.handle(
                register_command(other_tenant, key_hash=shared)
            )
        finally:
            await handler.close()
        assert await _count_keys(dsn) == 1
    assert first.outcome is Outcome.REGISTERED
    assert other_name.outcome is Outcome.REFUSED_HASH_COLLISION
    assert other_owner.outcome is Outcome.REFUSED_HASH_COLLISION
    assert other_owner.credential_id is None


async def test_register_same_name_and_hash_with_another_expiry_is_a_name_conflict() -> (
    None
):
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        try:
            await handler.handle(register_command(tenant, expires_at=in_hours(2)))
            changed = await handler.handle(
                register_command(tenant, expires_at=in_hours(3))
            )
        finally:
            await handler.close()
    assert changed.outcome is Outcome.REFUSED_NAME_CONFLICT


async def test_register_replay_after_revoke_never_reactivates_the_row() -> None:
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        registrar, revoker = register_handler(dsn), revoke_handler(dsn)
        try:
            first = await registrar.handle(register_command(tenant))
            assert first.credential_id is not None
            await revoker.handle(revoke_command(tenant, first.credential_id))
            replay = await registrar.handle(register_command(tenant))
        finally:
            await registrar.close()
            await revoker.close()
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow(
                "SELECT is_active, revoked_at FROM tenant_api_keys"
            )
        finally:
            await conn.close()
    assert replay.outcome is Outcome.REFUSED_HASH_COLLISION
    assert row is not None
    assert row["is_active"] is False
    assert row["revoked_at"] is not None


async def test_register_an_expired_name_is_held_until_revoked() -> None:
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        await add_key(
            dsn,
            tenant,
            name="laptop",
            key_hash=digest("old"),
            expires_at=in_hours(-1),
        )
        handler = register_handler(dsn)
        try:
            remint = await handler.handle(
                register_command(tenant, key_hash=digest("new"))
            )
        finally:
            await handler.close()
        assert await _count_keys(dsn) == 1
    assert remint.outcome is Outcome.REFUSED_NAME_CONFLICT


async def test_register_replay_after_the_original_expiry_still_answers_already_registered() -> (
    None
):
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        expired = in_hours(-1)
        credential = await add_key(
            dsn,
            tenant,
            name="laptop",
            key_hash=digest(f"{tenant}/laptop"),
            expires_at=expired,
        )
        handler = register_handler(dsn)
        try:
            replay = await handler.handle(register_command(tenant, expires_at=expired))
        finally:
            await handler.close()
    assert replay.outcome is Outcome.ALREADY_REGISTERED
    assert replay.credential_id == credential


@pytest.mark.parametrize(
    "key_hash",
    ["", "abc", "A" * 64, "g" * 64, "0" * 63, "0" * 65, ("0" * 64) + "\n"],
)
async def test_register_refuses_a_key_hash_that_is_not_64_lowercase_hex(
    key_hash: str,
) -> None:
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        try:
            result = await handler.handle(register_command(tenant, key_hash=key_hash))
        finally:
            await handler.close()
        assert await _count_keys(dsn) == 0
    assert result.outcome is Outcome.REFUSED_INVALID_KEY_HASH


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Laptop",
        "-lead",
        "has_underscore",
        "onxk_credential",
        "a" * 64,
        "sp ace",
        "name\n",
    ],
)
async def test_register_refuses_a_name_that_is_not_a_slug(name: str) -> None:
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        try:
            result = await handler.handle(register_command(tenant, name=name))
        finally:
            await handler.close()
        assert await _count_keys(dsn) == 0
    assert result.outcome is Outcome.REFUSED_INVALID_NAME


async def test_register_accepts_the_longest_slug_name() -> None:
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        try:
            result = await handler.handle(register_command(tenant, name="a" * 63))
        finally:
            await handler.close()
    assert result.outcome is Outcome.REGISTERED


async def test_register_refuses_an_expiry_that_is_not_strictly_in_the_database_future() -> (
    None
):
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        try:
            past = await handler.handle(
                register_command(tenant, expires_at=in_hours(-0.01))
            )
        finally:
            await handler.close()
        assert await _count_keys(dsn) == 0
    assert past.outcome is Outcome.REFUSED_EXPIRY_NOT_IN_FUTURE


async def test_register_the_hosted_overlay_bounds_every_key_and_refuses_perpetual() -> (
    None
):
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn, HOSTED_OVERLAY)
        try:
            perpetual = await handler.handle(register_command(tenant, name="p"))
            beyond = await handler.handle(
                register_command(tenant, name="b", expires_at=in_hours(24 * 91))
            )
            inside = await handler.handle(
                register_command(tenant, name="i", expires_at=in_hours(24 * 89))
            )
        finally:
            await handler.close()
        assert await _count_keys(dsn) == 1
    assert perpetual.outcome is Outcome.REFUSED_EXPIRY_REQUIRED
    assert beyond.outcome is Outcome.REFUSED_EXPIRY_BEYOND_MAX_LIFETIME
    assert inside.outcome is Outcome.REGISTERED


async def test_register_the_dev_overlay_allows_a_perpetual_key() -> None:
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn, DEV_OVERLAY)
        try:
            result = await handler.handle(register_command(tenant))
        finally:
            await handler.close()
    assert result.outcome is Outcome.REGISTERED
    assert result.expires_at is None


async def test_register_refuses_an_absent_tenant_and_a_tenant_that_is_not_active() -> (
    None
):
    async with scratch_identity_database() as dsn:
        suspended = await add_tenant(dsn, state="suspended")
        absent = UUID(int=7)
        handler = register_handler(dsn)
        try:
            held = await handler.handle(register_command(suspended))
            missing = await handler.handle(register_command(absent))
        finally:
            await handler.close()
        assert await _count_keys(dsn) == 0
    assert held.outcome is Outcome.REFUSED_TENANT_NOT_ACTIVE
    assert missing.outcome is Outcome.REFUSED_TENANT_NOT_ACTIVE


async def test_register_two_connections_with_one_command_insert_once_and_one_answers_already_registered() -> (
    None
):
    async with scratch_identity_database() as dsn:
        for round_number in range(5):
            tenant = await add_tenant(dsn)
            command = register_command(tenant, name=f"race-{round_number}")
            first, second = register_handler(dsn), register_handler(dsn)
            try:
                results = await asyncio.gather(
                    first.handle(command), second.handle(command)
                )
            finally:
                await first.close()
                await second.close()
            outcomes = sorted(r.outcome.value for r in results)
            assert outcomes == ["already_registered", "registered"]
            assert results[0].credential_id == results[1].credential_id
        assert await _count_keys(dsn) == 5


class _StaleFirstReadStore:
    """The loser of a race: its first digest lookup ran before the winner committed and found nothing."""

    def __init__(self, real: ProtocolTenantApiKeyStore) -> None:
        self._real = real
        self._reads = 0

    async def read_by_key_hash(self, key_hash: str) -> ModelTenantApiKeyRow | None:
        self._reads += 1
        return None if self._reads == 1 else await self._real.read_by_key_hash(key_hash)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


async def test_register_the_loser_of_a_unique_violation_rereads_the_winner_by_hash() -> (
    None
):
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        winner = register_handler(dsn)
        real = store_for(dsn)
        loser = register_handler(dsn)
        loser._store = _StaleFirstReadStore(real)  # type: ignore[assignment]
        command = register_command(tenant)
        try:
            won = await winner.handle(command)
            lost = await loser.handle(command)
        finally:
            await winner.close()
            await real.close()
        assert await _count_keys(dsn) == 1
    assert won.outcome is Outcome.REGISTERED
    assert lost.outcome is Outcome.ALREADY_REGISTERED
    assert lost.credential_id == won.credential_id


async def test_register_the_loser_of_an_active_name_race_with_another_hash_is_a_name_conflict() -> (
    None
):
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        winner = register_handler(dsn)
        real = store_for(dsn)
        loser = register_handler(dsn)
        loser._store = _StaleFirstReadStore(real)  # type: ignore[assignment]
        try:
            await winner.handle(register_command(tenant))
            lost = await loser.handle(
                register_command(tenant, key_hash=digest("other"))
            )
        finally:
            await winner.close()
            await real.close()
    assert lost.outcome is Outcome.REFUSED_NAME_CONFLICT


async def test_register_answers_schema_outdated_when_the_expires_at_column_is_absent() -> (
    None
):
    async with scratch_identity_database(with_expires_at=False) as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        try:
            result = await handler.handle(register_command(tenant))
        finally:
            await handler.close()
    assert result.outcome is Outcome.STORE_SCHEMA_OUTDATED


async def test_register_answers_store_unavailable_when_the_database_is_unreachable() -> (
    None
):
    handler = register_handler("postgresql://nobody@127.0.0.1:1/none")
    try:
        result = await handler.handle(register_command(UUID(int=1)))
    finally:
        await handler.close()
    assert result.outcome is Outcome.STORE_UNAVAILABLE


async def test_register_never_lets_a_key_shaped_input_reach_a_result_a_refusal_or_a_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plaintext_canary = "onxk_PLAINTEXT-CANARY-0123456789abcdef"
    digest_canary = digest("DIGEST-CANARY")
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        try:
            with caplog.at_level(logging.DEBUG):
                outcomes = [
                    # a key where the name belongs, a key where the digest belongs
                    await handler.handle(
                        register_command(tenant, name=plaintext_canary)
                    ),
                    await handler.handle(
                        register_command(tenant, key_hash=plaintext_canary)
                    ),
                    await handler.handle(
                        register_command(
                            tenant, name=plaintext_canary, key_hash=digest_canary
                        )
                    ),
                    # a valid digest, registered, replayed and collided
                    await handler.handle(
                        register_command(tenant, key_hash=digest_canary)
                    ),
                    await handler.handle(
                        register_command(tenant, key_hash=digest_canary)
                    ),
                    await handler.handle(
                        register_command(tenant, name="other", key_hash=digest_canary)
                    ),
                ]
        finally:
            await handler.close()
    observed = " ".join(r.getMessage() for r in caplog.records) + " ".join(
        r.model_dump_json() for r in outcomes
    )
    assert plaintext_canary not in observed
    assert plaintext_canary.lower() not in observed.lower()
    assert digest_canary not in observed
    assert [r.outcome.value for r in outcomes] == [
        "refused_invalid_name",
        "refused_invalid_key_hash",
        "refused_invalid_name",
        "registered",
        "already_registered",
        "refused_hash_collision",
    ]


def test_register_a_request_with_a_stray_field_is_refused_without_echoing_the_value() -> (
    None
):
    from pydantic import ValidationError

    from omnimarket.nodes.node_tenant_api_key_effect.models import (
        ModelTenantApiKeyRegisterRequest,
    )

    secret_value = "onxk_STRAY-FIELD-CANARY"
    with pytest.raises(ValidationError) as caught:
        ModelTenantApiKeyRegisterRequest.model_validate(
            {
                "correlation_id": "00000000-0000-4000-8000-000000000001",
                "tenant_id": "00000000-0000-4000-8000-000000000002",
                "name": "laptop",
                "key_hash": digest("x"),
                "plain": secret_value,
            }
        )
    assert secret_value not in str(caught.value)
