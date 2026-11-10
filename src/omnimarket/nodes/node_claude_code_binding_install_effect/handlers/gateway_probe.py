# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Non-inference validation of an authenticated ONEX gateway's declared protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_code_binding_install_effect.models.model_claude_gateway_settings import (
    ModelClaudeGatewaySettingsRequest,
)


def probe_gateway(
    request: ModelClaudeGatewaySettingsRequest,
    *,
    read_secret: Callable[[str], Awaitable[str | None]] | None = None,
    client: httpx.Client | None = None,
) -> bool:
    """No model inference, redirects, proxy inheritance, logging or secret-bearing URLs."""
    owned_client = client is None
    try:
        if (
            request.gateway_url is None
            or request.credential_ref is None
            or request.model is None
        ):
            return False
        reader = read_secret or LocalByokCredentialStore().get_secret

        async def read_value() -> str | None:
            return await reader(request.credential_ref or "")

        secret = asyncio.run(read_value())
        if not secret or any(ord(c) < 32 or ord(c) == 127 for c in secret):
            return False
        if client is None:
            client = httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        headers = {"Authorization": f"Bearer {secret}"}
        protocol = client.get(
            request.gateway_url + "/onex/gateway/protocol", headers=headers
        )
        if protocol.status_code != 200:
            return False
        declared = protocol.json()
        if (
            not isinstance(declared, dict)
            or declared.get("protocol") != "onex-claude-gateway.v1"
            or any(
                declared.get(key) is not True
                for key in ("streaming", "tool_use", "receipts")
            )
        ):
            return False
        models = client.get(request.gateway_url + "/v1/models", headers=headers)
        if models.status_code != 200:
            return False
        body = models.json()
        data = body.get("data") if isinstance(body, dict) else None
        return isinstance(data, list) and any(
            isinstance(item, dict) and item.get("id") == request.model for item in data
        )
    except Exception:
        return False
    finally:
        if owned_client and client is not None:
            client.close()
