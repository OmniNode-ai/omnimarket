# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: contract -> mounted app -> typed handler -> repository -> answer.

node_hook_events_by_correlation_read_effect. The chain is walked from the
contract's own route and handler binding, not from hard-coded names.
"""

from __future__ import annotations

import importlib

import pytest
import yaml

from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers.http_dependencies import (
    CONTRACT_PATH,
)
from tests.nodes.node_hook_events_by_correlation_read_effect.support import (
    CID,
    KEY_A,
    SLUG_A,
    FakeReader,
    client,
    row,
)

pytestmark = pytest.mark.unit


async def test_golden_chain_from_contract_route_to_tenant_scoped_answer():
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    route = contract["http_route"]
    module = importlib.import_module(route["handler"]["module"])
    assert hasattr(module, route["handler"]["name"])
    factory_module, _, factory = contract["http_mount"]["factory"].partition(":")
    assert callable(getattr(importlib.import_module(factory_module), factory))

    reader = FakeReader([row(SLUG_A)])
    async with client(reader) as c:
        resp = await c.get(
            route["path"], params={"correlation_id": CID}, headers={"x-api-key": KEY_A}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data_state"] == "found"
    assert body["tenant_scope"] == SLUG_A
    assert reader.calls[0]["tenant_slug"] == SLUG_A
