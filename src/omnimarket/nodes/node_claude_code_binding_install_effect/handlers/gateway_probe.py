# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Non-inference validation of an authenticated ONEX gateway's declared protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_code_binding_install_effect.models.model_claude_gateway_settings import (
    ModelClaudeGatewaySettingsRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    get_provider_json,
)


def probe_gateway(
    request: ModelClaudeGatewaySettingsRequest,
    *,
    read_secret: Callable[[str], Awaitable[str | None]] | None = None,
    get: Callable[..., object] | None = None,
) -> bool:
    """No model inference, redirects, proxy inheritance, logging or secret-bearing URLs."""
    fetch = get or get_provider_json
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
        headers = {"Authorization": f"Bearer {secret}"}
        declared = fetch(
            url=request.gateway_url + "/onex/gateway/protocol",
            extra_headers=headers,
            timeout_seconds=10,
            trust_env=False,
        )
        if (
            not isinstance(declared, dict)
            or declared.get("protocol") != "onex-claude-gateway.v1"
            or any(
                declared.get(key) is not True
                for key in ("streaming", "tool_use", "receipts")
            )
        ):
            return False
        body = fetch(
            url=request.gateway_url + "/v1/models",
            extra_headers=headers,
            timeout_seconds=10,
            trust_env=False,
        )
        data = body.get("data") if isinstance(body, dict) else None
        return isinstance(data, list) and any(
            isinstance(item, dict) and item.get("id") == request.model for item in data
        )
    except Exception:
        return False
