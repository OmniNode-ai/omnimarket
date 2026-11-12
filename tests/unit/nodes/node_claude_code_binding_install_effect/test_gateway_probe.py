# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A live/authenticated ONEX protocol and exact model are required before enable."""

from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest

from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.gateway_probe import (
    probe_gateway,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.models.model_claude_gateway_settings import (
    ModelClaudeGatewaySettingsRequest,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "secret",
        "auth",
        "redirect",
        "model",
        "protocol",
        "streaming",
        "tool_use",
        "receipts",
    ],
)
def test_probe_requires_authenticated_protocol_and_model(failure: str) -> None:
    calls: list[str] = []

    async def secret(ref: str) -> str | None:
        assert ref == "gateway.fixture"
        return None if failure == "secret" else "SENTINEL-private"

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["authorization"] == "Bearer SENTINEL-private"
        if failure == "auth":
            return httpx.Response(401)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://other.example"})
        if request.url.path == "/onex/gateway/protocol":
            data: dict[str, object] = {
                "protocol": "onex-claude-gateway.v1",
                "streaming": True,
                "tool_use": True,
                "receipts": True,
            }
            if failure == "protocol":
                data["protocol"] = "other"
            elif failure in data:
                data[failure] = False
            return httpx.Response(200, json=data)
        return httpx.Response(
            200,
            json={
                "data": [{"id": "wrong" if failure == "model" else "claude-fixture"}]
            },
        )

    request = ModelClaudeGatewaySettingsRequest(
        action="enable",
        gateway_url="http://127.0.0.1:8181",
        model="claude-fixture",
        credential_ref="gateway.fixture",
        credential_helper="safe-helper",
    )

    def get(*, url, timeout_seconds, extra_headers, trust_env):
        assert trust_env is False
        assert timeout_seconds == 10
        req = httpx.Request("GET", url, headers=extra_headers)
        response = respond(req)
        response.request = req
        response.raise_for_status()
        return response.json()

    result = probe_gateway(request, read_secret=secret, get=get)
    assert result is (failure == "none")
    if failure == "secret":
        assert calls == []
    assert all("SENTINEL" not in url for url in calls)


def test_canonical_json_transport_can_refuse_ambient_proxies(monkeypatch):
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport

    options = []

    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        @contextmanager
        def stream(self, method, url, headers):
            yield httpx.Response(
                200,
                json={"data": []},
                request=httpx.Request(method, url, headers=headers),
            )

    monkeypatch.setattr(transport.httpx, "Client", Client)
    assert transport.get_provider_json(
        url="https://fixture.test/catalog", timeout_seconds=1, trust_env=False
    ) == {"data": []}
    assert options == [{"timeout": 1, "trust_env": False, "follow_redirects": False}]
