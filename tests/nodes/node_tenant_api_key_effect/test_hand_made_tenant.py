# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26474: the hand-made dev tenant's shape (an active tenant with an existing key named hermes) is untouched.

A register or a revoke for another tenant changes no row of that tenant and no row of its key.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_tenant_api_key_effect.models import (
    EnumTenantApiKeyRegisterOutcome as Registered,
)
from omnimarket.nodes.node_tenant_api_key_effect.models import (
    EnumTenantApiKeyRevokeOutcome as Revoked,
)
from tests.nodes.node_tenant_api_key_effect.support import (
    digest,
    register_command,
    register_handler,
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


async def test_hand_made_tenant_rows_are_unchanged_by_a_register_for_another_tenant() -> (
    None
):
    async with scratch_identity_database() as dsn:
        hand_made = await add_tenant(dsn, slug="hermes-test-20261122")
        hermes = await add_key(
            dsn, hand_made, name="hermes", key_hash=digest("hermes-key")
        )
        others = [await add_tenant(dsn) for _ in range(3)]
        before = await snapshot(dsn)
        registrar = register_handler(dsn)
        try:
            # another tenant takes the very same name: the active-name index is per tenant, so it succeeds
            same_name = await registrar.handle(
                register_command(others[0], name="hermes", key_hash=digest("other-key"))
            )
            fresh = await registrar.handle(register_command(others[1], name="laptop"))
            refused = await registrar.handle(
                register_command(others[2], name="clash", key_hash=digest("hermes-key"))
            )
        finally:
            await registrar.close()
        after = await snapshot(dsn)
    assert same_name.outcome is Registered.REGISTERED
    assert fresh.outcome is Registered.REGISTERED
    assert refused.outcome is Registered.REFUSED_HASH_COLLISION
    # no row changed: the new state is the old rows, byte for byte, plus exactly the two inserted keys
    assert all(row in after for row in before)
    assert len(after) == len(before) + 2
    hermes_rows = [row for row in before if row[0] == hermes or row[0] == hand_made]
    assert len(hermes_rows) == 2
    assert all(row in after for row in hermes_rows)


async def test_hand_made_tenant_key_is_untouched_by_a_revoke_for_another_tenant() -> (
    None
):
    async with scratch_identity_database() as dsn:
        hand_made = await add_tenant(dsn, slug="hermes-test-20261122")
        hermes = await add_key(
            dsn, hand_made, name="hermes", key_hash=digest("hermes-key")
        )
        other = await add_tenant(dsn)
        before = await snapshot(dsn)
        revoker = revoke_handler(dsn)
        try:
            result = await revoker.handle(revoke_command(other, hermes))
        finally:
            await revoker.close()
        after = await snapshot(dsn)
    assert result.outcome is Revoked.NOT_FOUND
    assert after == before
