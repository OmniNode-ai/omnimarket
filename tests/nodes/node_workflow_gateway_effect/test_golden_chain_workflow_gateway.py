# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: contract -> declared handler bindings -> mounted routes -> submit, status, receipt.

Exemplar: tests/nodes/node_plan_list/test_golden_chain_plan_list.py (contract-declared chain).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import yaml

from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from tests.nodes.node_workflow_gateway_effect.fakes import (
    API_KEY_HEADER,
    KEY_A,
    SKILL_PAYLOAD,
    SKILL_TOPIC,
    TENANT_A,
    Gateway,
    build_gateway,
)

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_workflow_gateway_effect/contract.yaml"
)


def _client(gateway: Gateway) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(dependencies=gateway.deps))
    return httpx.AsyncClient(transport=transport, base_url="http://runtime")


def test_contract_declares_every_route_the_mount_serves() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert contract["http_mount"]["path_prefix"] == "/v1/workflows"
    routes = {(r["method"], r["path"]) for r in contract["http_routes"]}
    assert routes == {
        ("POST", "/v1/workflows"),
        ("GET", "/v1/workflows/contracts"),
        ("GET", "/v1/workflows/{workflow_id}/status"),
        ("GET", "/v1/workflows/{workflow_id}/receipt"),
    }
    for route in contract["http_routes"]:
        module = importlib.import_module(route["handler"]["module"])
        assert getattr(module, route["handler"]["name"]) is not None
    request = contract["input_model"]
    assert getattr(importlib.import_module(request["module"]), request["name"])


async def test_submit_status_and_receipt_follow_one_workflow_to_terminal() -> None:
    gateway = build_gateway()
    async with _client(gateway) as client:
        contracts = await client.get(
            "/v1/workflows/contracts", headers={API_KEY_HEADER: KEY_A}
        )
        assert contracts.status_code == 200
        advertised = [c["workflow_type"] for c in contracts.json()["contracts"]]
        assert advertised == [
            "delegate-skill",
            "delegation-inference",
        ]  # fenced one absent

        submitted = await client.post(
            "/v1/workflows",
            headers={API_KEY_HEADER: KEY_A},
            json={"workflow_type": "delegate-skill", "payload": SKILL_PAYLOAD},
        )
        assert submitted.status_code == 202
        ack = submitted.json()
        assert ack["status"] == "published"
        assert ack["workflow_type"] == "delegate-skill"
        workflow_id = ack["workflow_id"]

        # The command was published once, on the catalog's topic, keyed by the workflow id.
        assert len(gateway.publisher.published) == 1
        topic, key, wire = gateway.publisher.published[0]
        assert (topic, key) == (SKILL_TOPIC, workflow_id)
        assert wire["correlation_id"] == ack["correlation_id"]
        assert wire["payload"]["tenant_id"] == str(TENANT_A)

        status = await client.get(
            f"/v1/workflows/{workflow_id}/status", headers={API_KEY_HEADER: KEY_A}
        )
        assert status.status_code == 200
        assert status.json()["status"] == "published"
        assert status.json()["terminal_model_used"] is None

        # Not terminal yet: no receipt.
        early = await client.get(
            f"/v1/workflows/{workflow_id}/receipt",
            params={"runner_identity": "ci-run-1"},
            headers={API_KEY_HEADER: KEY_A},
        )
        assert early.status_code == 409

        gateway.store.apply_terminal(
            UUID(workflow_id),
            status="completed",
            terminal_model_used="qwen",
            terminal_total_tokens=12,
            terminal_latency_ms=340,
            result_content="alive",
        )
        done = await client.get(
            f"/v1/workflows/{workflow_id}/status", headers={API_KEY_HEADER: KEY_A}
        )
        assert done.json()["status"] == "completed"
        assert done.json()["terminal_model_used"] == "qwen"

        receipt = await client.get(
            f"/v1/workflows/{workflow_id}/receipt",
            params={"runner_identity": "ci-run-1"},
            headers={API_KEY_HEADER: KEY_A},
        )
        assert receipt.status_code == 200
        body = receipt.json()
        assert body["verifier"] == "ci-run-1"
        assert body["status"] == "completed"
        assert body["tenant_id"] == str(TENANT_A)
        assert body["event_count"] == 1
        assert len(body["projection_row_hash"]) == 64
        assert len(body["terminal_event_hash"]) == 64
