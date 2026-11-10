# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: contract route and handler binding -> overlay-bound mount -> typed answer."""

from __future__ import annotations

import importlib
import json

import pytest

from tests.nodes.tenant_read_support import (
    KEY_A,
    TENANT_A,
    client_for,
    contract_of,
    installed,
    mounted_factory,
    overlay,
)

pytestmark = pytest.mark.unit
NODE = "node_tenant_savings_read_effect"


async def test_contract_binding_resolves_to_the_handler_and_serves_through_the_mount() -> (
    None
):
    contract = contract_of(NODE)
    binding = contract["handler_routing"]["handlers"][0]["handler"]
    assert binding == contract["http_route"]["handler"]
    handler_class = getattr(importlib.import_module(binding["module"]), binding["name"])
    assert handler_class.__name__ == "HandlerTenantSavingsRead"

    seed = json.dumps(
        {str(TENANT_A): {"savings": {"total_saved_usd": 4.0, "delegation_runs": 1}}}
    )
    bindings = overlay(
        identity={KEY_A: f"{TENANT_A},test-tenant,principal-a"}, reader_seed=seed
    )
    with installed(bindings):
        app = mounted_factory(NODE)()
    async with client_for(app) as client:
        response = await client.get(
            contract["http_route"]["path"], headers={"x-api-key": KEY_A}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["total_saved_usd"] == 4.0
