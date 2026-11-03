# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: contract -> declared handler binding -> mounted route -> answer."""

from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest
import yaml

from omnimarket.nodes.node_usage_privileges.handlers.http_app import create_app
from tests.nodes.node_usage_privileges.fakes import (
    SEEDED_API_KEY,
    FakeUsagePrivilegeRepository,
    SeededTenantIdentity,
)

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_usage_privileges/contract.yaml"
)


async def test_contract_declared_chain_answers_for_a_resolved_caller() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    entry = contract["handler_routing"]["handlers"][0]
    module = importlib.import_module(entry["handler"]["module"])
    assert getattr(module, entry["handler"]["name"]) is not None
    model = importlib.import_module(entry["event_model"]["module"])
    assert getattr(model, entry["event_model"]["name"]) is not None
    transport = httpx.ASGITransport(
        app=create_app(
            repository=FakeUsagePrivilegeRepository(), identity=SeededTenantIdentity()
        )
    )
    route = contract["http_route"]
    async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as c:
        response = await c.request(
            route["method"], route["path"], headers={"x-api-key": SEEDED_API_KEY}
        )
    assert response.status_code == 200
    assert [row["plan_tier_code"] for row in response.json()] == ["free", "pro"]
