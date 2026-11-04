# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three contracts declare one mount and carry their routes."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from omnimarket.models.model_inference_credential_create_request import (
    ModelInferenceCredentialCreateRequest,
)
from tests.nodes.inference_credential_http_support import CONTRACTS, ROUTE

pytestmark = pytest.mark.unit


def _load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(CONTRACTS[name].read_text())
    return loaded


def test_exactly_one_contract_declares_the_mount_and_its_prefix_is_the_route_path() -> (
    None
):
    mounts = {n: _load(n).get("http_mount") for n in CONTRACTS}
    declared = {n: m for n, m in mounts.items() if m}
    assert list(declared) == ["create"]
    assert declared["create"]["path_prefix"] == ROUTE
    assert declared["create"]["factory"].endswith("handlers.http_app:create_app")


def test_the_routes_are_the_three_the_gateway_binds() -> None:
    routes = {
        n: (_load(n)["http_route"]["method"], _load(n)["http_route"]["path"])
        for n in CONTRACTS
    }
    assert routes == {
        "create": ("POST", ROUTE),
        "list": ("GET", ROUTE),
        "revoke": ("DELETE", f"{ROUTE}/{{api_key_ref}}"),
    }
    for name in CONTRACTS:
        assert _load(name)["name"] == f"node_inference_credential_{name}"


def test_the_post_input_schema_forbids_a_caller_supplied_ref_or_tenant() -> None:
    schema = _load("create")["http_route"]["input_schema"]
    assert schema["additionalProperties"] is False
    assert "api_key_ref" not in schema["properties"]
    assert "tenant_id" not in schema["properties"]
    assert set(schema["required"]) <= set(
        ModelInferenceCredentialCreateRequest.model_fields
    )
    assert set(schema["properties"]) <= set(
        ModelInferenceCredentialCreateRequest.model_fields
    )
    assert ModelInferenceCredentialCreateRequest.model_config["extra"] == "forbid"
