# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: contract-declared route -> mounted app -> handler -> adapters (revoke)."""

from __future__ import annotations

import pytest

from tests.nodes.inference_credential_http_support import (
    ROUTE,
    TENANT_A,
    World,
    fake_row,
)

pytestmark = pytest.mark.unit


def test_golden_chain_inference_credential_revoke() -> None:
    row = fake_row(TENANT_A, "abc")
    world = World(rows=[row])
    response = world.client.delete(
        f"{ROUTE}/{row['api_key_ref']}", headers=world.as_tenant_a()
    )
    assert response.status_code == 200
    [(topic, _, _)] = world.bus.published
    assert topic == "onex.evt.omnimarket.credential-revoked.v1"
