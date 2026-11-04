# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: contract-declared route -> mounted app -> handler -> adapters (create)."""

from __future__ import annotations

import pytest

from tests.nodes.inference_credential_http_support import (
    GOOD_BODY,
    IDEMPOTENCY,
    ROUTE,
    World,
)

pytestmark = pytest.mark.unit


def test_golden_chain_inference_credential_create() -> None:
    world = World()
    response = world.client.post(
        ROUTE, json=GOOD_BODY, headers=world.as_tenant_a(**IDEMPOTENCY)
    )
    assert response.status_code == 201
    [(topic, _, _)] = world.bus.published
    assert topic == "onex.evt.omnimarket.credential-registered.v1"
