# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: contract -> declared handler binding -> mounted routes -> answers.

The three declared routes answer a resolved caller: submit returns the ref and
metadata (never the value), list returns only the caller's own refs, revoke answers
for a ref the caller owns.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest
import yaml

from omnimarket.nodes.node_inference_credentials.handlers.http_app import create_app
from tests.nodes.node_inference_credentials.fakes import (
    CREATE_BODY,
    KEY_A,
    REF_A,
    SECRET_VALUE,
    TENANT_A,
    FakeIntake,
    FakeReadModel,
    SeededTenantIdentity,
)

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_inference_credentials/contract.yaml"
)
PREFIX = "/v1/tenants/me/inference-credentials"


def _client(intake: FakeIntake, read_model: FakeReadModel) -> httpx.AsyncClient:
    app = create_app(
        intake=intake, read_model=read_model, identity=SeededTenantIdentity()
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    )


def test_contract_declares_the_mount_the_routes_and_a_resolvable_handler() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    handler = contract["http_handler"]
    module = importlib.import_module(handler["module"])
    assert getattr(module, handler["name"]) is not None
    factory_module, _, factory = contract["http_mount"]["factory"].partition(":")
    assert getattr(importlib.import_module(factory_module), factory) is create_app
    assert contract["http_mount"]["path_prefix"] == PREFIX
    served = {(r["method"], r["path"]) for r in contract["http_routes"]}
    assert served == {
        ("POST", PREFIX),
        ("GET", PREFIX),
        ("DELETE", f"{PREFIX}/{{api_key_ref}}"),
    }
    assert all(path.startswith(PREFIX) for _, path in served)


async def test_submit_answers_201_with_ref_and_metadata_and_never_the_value() -> None:
    intake = FakeIntake()
    async with _client(intake, FakeReadModel()) as client:
        response = await client.post(
            PREFIX, content=CREATE_BODY, headers={"x-api-key": KEY_A}
        )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "mine"
    assert body["provider"] == "openrouter"
    assert body["api_key_ref"].startswith(f"cred_{TENANT_A.tenant_id}_openrouter_")
    assert "key_value" not in body
    assert SECRET_VALUE not in response.text
    assert intake.registered == [(str(TENANT_A.tenant_id), "openrouter")]


async def test_list_answers_only_the_callers_own_refs() -> None:
    async with _client(FakeIntake(), FakeReadModel()) as client:
        response = await client.get(PREFIX, headers={"x-api-key": KEY_A})
    assert response.status_code == 200
    assert [row["api_key_ref"] for row in response.json()] == [REF_A]
    assert set(response.json()[0]) == {
        "api_key_ref",
        "name",
        "provider",
        "created_at",
        "revoked_at",
    }


async def test_revoke_of_an_owned_ref_answers_200_and_publishes_once() -> None:
    intake = FakeIntake()
    async with _client(intake, FakeReadModel()) as client:
        response = await client.delete(
            f"{PREFIX}/{REF_A}", headers={"x-api-key": KEY_A}
        )
    assert response.status_code == 200
    assert response.json() == {"api_key_ref": REF_A, "status": "revocation-published"}
    assert intake.revoked == [(str(TENANT_A.tenant_id), REF_A)]
