# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-request routing through an ordered native-Messages ladder (OMN-23413 Amendment 1).

Claude Code sees one model name. For each request the gateway tries its primary
backend, then each configured fallback in order, and only among backends that
serve native Anthropic Messages.

Failure modes:
  F1. The primary is down (503) and the request fails although a second native
      backend is configured: no fallback.
  F2. The fallback receives the request still naming the primary's model, so the
      second server refuses or serves the wrong model.
  F3. The receipt names the primary although the fallback answered, so the
      dashboard shows the wrong backend and model.
  F4. A fallback that is not native Messages (an OpenAI chat-completions URL) is
      called, silently dropping Claude fields.
  F5. A client error (400) from the primary is retried on the next backend,
      hiding a bad request behind a different model.
  F6. A healthy primary still touches the fallback.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_claude_gateway import (
    build_gateway_app,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_claude_gateway import (
    ModelClaudeGatewayConfig,
    ModelClaudeGatewayReceipt,
)
from tests.helpers.asgi_ingress import AsgiIngress

pytestmark = pytest.mark.unit
AUTH = {"authorization": "Bearer gateway-SENTINEL"}
BODY = {
    "model": "claude-fixture",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hello"}],
    "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
}
PRIMARY = "https://primary.example/v1/messages"
SECOND = "https://second.example/v1/messages"
CHAT = "https://chat.example/v1/chat/completions"


def reply(model: str) -> dict[str, object]:
    return {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": model,
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "answer"}],
        "usage": {"input_tokens": 3, "output_tokens": 5},
    }


def native(backend_id: str, model: str, url: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=backend_id,
        model_id=model,
        endpoint_ref=url,
        max_tokens=4096,
        timeout_ms=30000,
        secret_ref="upstream.fixture",
    )


def application(
    respond: Callable[[httpx.Request], httpx.Response],
    receipts: list[ModelClaudeGatewayReceipt],
    fallback_url: str = SECOND,
):
    async def read_secret(reference: str) -> str | None:
        return {
            "gateway.fixture": "gateway-SENTINEL",
            "upstream.fixture": "upstream-SENTINEL",
        }.get(reference)

    async def record(receipt: ModelClaudeGatewayReceipt) -> None:
        receipts.append(receipt)

    return build_gateway_app(
        ModelClaudeGatewayConfig(
            backend_id="fixture-primary",
            model="claude-fixture",
            credential_ref="gateway.fixture",
            fallback_backend_ids=("fixture-second",),
        ),
        resolve_backend=lambda: native("fixture-primary", "claude-fixture", PRIMARY),
        resolve_fallbacks=lambda: (
            native("fixture-second", "model-second", fallback_url),
        ),
        read_secret=read_secret,
        record=record,
        transport=httpx.MockTransport(respond),
    )


def upstream(primary_status: int, calls: list[httpx.Request]):
    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "primary.example":
            if primary_status != 200:
                return httpx.Response(primary_status, json={"error": "fixture"})
            return httpx.Response(200, json=reply("claude-fixture"))
        return httpx.Response(200, json=reply("model-second"))

    return respond


@pytest.mark.asyncio
async def test_ladder_primary_down_second_native_backend_serves_with_its_receipt() -> (
    None
):
    """F1, F2, F3."""
    calls: list[httpx.Request] = []
    receipts: list[ModelClaudeGatewayReceipt] = []
    async with AsgiIngress(application(upstream(503, calls), receipts)) as client:
        response = await client.invoke("POST", "/v1/messages", json=BODY, headers=AUTH)
    assert response.status_code == 200
    assert [call.url.host for call in calls] == ["primary.example", "second.example"]
    forwarded = json.loads(calls[1].content)
    assert forwarded["model"] == "model-second"
    assert forwarded["tools"] == BODY["tools"]
    assert forwarded["messages"] == BODY["messages"]
    assert calls[1].headers["x-api-key"] == "upstream-SENTINEL"
    assert [(r.backend_id, r.model, r.status) for r in receipts] == [
        ("fixture-second", "model-second", "completed")
    ]
    assert response.headers["x-onex-run-id"] == str(receipts[0].run_id)


@pytest.mark.asyncio
async def test_ladder_never_calls_a_backend_that_is_not_native_messages() -> None:
    """F4: the only fallback is a chat-completions URL; the request fails visibly."""
    calls: list[httpx.Request] = []
    receipts: list[ModelClaudeGatewayReceipt] = []
    app = application(upstream(503, calls), receipts, fallback_url=CHAT)
    async with AsgiIngress(app) as client:
        response = await client.invoke("POST", "/v1/messages", json=BODY, headers=AUTH)
    assert response.status_code == 502
    assert [call.url.host for call in calls] == ["primary.example"]
    assert [(r.backend_id, r.status) for r in receipts] == [
        ("fixture-primary", "failed")
    ]


@pytest.mark.asyncio
async def test_ladder_client_error_is_not_retried_on_another_model() -> None:
    """F5."""
    calls: list[httpx.Request] = []
    receipts: list[ModelClaudeGatewayReceipt] = []
    async with AsgiIngress(application(upstream(400, calls), receipts)) as client:
        response = await client.invoke("POST", "/v1/messages", json=BODY, headers=AUTH)
    assert response.status_code == 502
    assert [call.url.host for call in calls] == ["primary.example"]
    assert [r.status for r in receipts] == ["failed"]


@pytest.mark.asyncio
async def test_ladder_healthy_primary_never_touches_the_fallback() -> None:
    """F6, and the control for F1: the primary's bytes go upstream unchanged."""
    calls: list[httpx.Request] = []
    receipts: list[ModelClaudeGatewayReceipt] = []
    raw = json.dumps(BODY).encode()
    async with AsgiIngress(application(upstream(200, calls), receipts)) as client:
        response = await client.invoke(
            "POST",
            "/v1/messages",
            content=raw,
            headers={**AUTH, "content-type": "application/json"},
        )
    assert response.status_code == 200
    assert [call.url.host for call in calls] == ["primary.example"]
    assert calls[0].content == raw
    assert [(r.backend_id, r.model) for r in receipts] == [
        ("fixture-primary", "claude-fixture")
    ]


def test_ladder_configuration_refuses_a_repeated_backend() -> None:
    for ladder in (("fixture-primary",), ("fixture-second", "fixture-second")):
        with pytest.raises(ValidationError):
            ModelClaudeGatewayConfig(
                backend_id="fixture-primary",
                model="claude-fixture",
                credential_ref="gateway.fixture",
                fallback_backend_ids=ladder,
            )
