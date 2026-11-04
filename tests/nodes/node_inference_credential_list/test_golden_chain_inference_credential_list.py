# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: contract-declared route -> mounted app -> handler -> adapters (list)."""

from __future__ import annotations

import pytest

from tests.nodes.inference_credential_http_support import (
    ROUTE,
    TENANT_A,
    World,
    fake_row,
)

pytestmark = pytest.mark.unit


def test_golden_chain_inference_credential_list() -> None:
    world = World(rows=[fake_row(TENANT_A, "abc")])
    response = world.client.get(ROUTE, headers=world.as_tenant_a())
    assert response.status_code == 200
    assert [i["api_key_ref"] for i in response.json()] == [
        f"cred_{TENANT_A}_openai_abc"
    ]
