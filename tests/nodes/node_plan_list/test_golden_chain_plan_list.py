# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: contract -> declared handler binding -> mounted route -> answer."""

from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest
import yaml

from omnimarket.nodes.node_plan_list.handlers.http_app import create_app
from tests.nodes.node_plan_list.fakes import FakePlanCatalogRepository

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_plan_list/contract.yaml"
)


async def test_contract_declared_chain_answers_the_catalog() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    entry = contract["handler_routing"]["handlers"][0]
    module = importlib.import_module(entry["handler"]["module"])
    assert getattr(module, entry["handler"]["name"]) is not None
    model = importlib.import_module(entry["event_model"]["module"])
    assert getattr(model, entry["event_model"]["name"]) is not None
    transport = httpx.ASGITransport(
        app=create_app(repository=FakePlanCatalogRepository())
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as c:
        response = await c.request(
            contract["http_route"]["method"], contract["http_route"]["path"]
        )
    assert response.status_code == 200
    assert [plan["code"] for plan in response.json()] == ["free", "pro", "team"]
