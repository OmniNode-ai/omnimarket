# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Native ingress controls: authentication, opaque wire data, streams and durable receipts."""

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

pytestmark = pytest.mark.unit
AUTH = {"authorization": "Bearer gateway-SENTINEL"}
BODY = {
    "model": "claude-fixture",
    "max_tokens": 512,
    "messages": [{"role": "user", "content": "hello"}],
}
REPLY = {
    "id": "msg_fixture",
    "type": "message",
    "role": "assistant",
    "model": "claude-fixture",
    "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "answer"}],
    "usage": {"input_tokens": 3, "output_tokens": 5, "cache_read_input_tokens": 2},
}


def config(**changes: object) -> ModelClaudeGatewayConfig:
    values: dict[str, object] = {
        "backend_id": "fixture-native",
        "model": "claude-fixture",
        "credential_ref": "gateway.fixture",
        "bind_host": "127.0.0.1",
        "bind_port": 8181,
    }
    values.update(changes)
    return ModelClaudeGatewayConfig.model_validate(values)


def backend(
    url: str = "https://upstream.example/v1/messages",
) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id="fixture-native",
        model_id="claude-fixture",
        endpoint_ref=url,
        max_tokens=4096,
        timeout_ms=30000,
        secret_ref="upstream.fixture",
    )


def application(
    respond: Callable[[httpx.Request], httpx.Response],
    receipts: list[ModelClaudeGatewayReceipt],
    *,
    fail_record: bool = False,
):
    async def read_secret(reference: str) -> str | None:
        return {
            "gateway.fixture": "gateway-SENTINEL",
            "upstream.fixture": "upstream-SENTINEL",
        }.get(reference)

    async def record(receipt: ModelClaudeGatewayReceipt) -> None:
        if fail_record:
            raise RuntimeError("SENTINEL-private-database-error")
        receipts.append(receipt)

    return build_gateway_app(
        config(),
        resolve_backend=backend,
        read_secret=read_secret,
        record=record,
        transport=httpx.MockTransport(respond),
    )


@pytest.mark.asyncio
async def test_authentication_precedes_all_upstream_io() -> None:
    calls: list[httpx.Request] = []
    app = application(
        lambda req: calls.append(req) or httpx.Response(200, json=REPLY), []
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        for route in ("/onex/gateway/protocol", "/v1/models"):
            assert (await client.get(route)).status_code == 401
        assert (await client.post("/v1/messages", json=BODY)).status_code == 401
        protocol = (await client.get("/onex/gateway/protocol", headers=AUTH)).json()
        assert protocol == {
            "protocol": "onex-claude-gateway.v1",
            "streaming": True,
            "tool_use": True,
            "receipts": True,
        }
        assert (await client.get("/v1/models", headers=AUTH)).json()["data"][0][
            "id"
        ] == "claude-fixture"
    assert calls == []


@pytest.mark.asyncio
async def test_complete_native_payload_headers_and_result_are_preserved() -> None:
    full = {
        **BODY,
        "system": [
            {"type": "text", "text": "system", "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "/example"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "data"}
                ],
            },
        ],
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        "thinking": {"type": "adaptive"},
        "new_native_option": {"keep": True},
    }
    seen: list[httpx.Request] = []

    def respond(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        assert str(req.url) == "https://upstream.example/v1/messages"
        assert json.loads(req.content) == full
        assert req.headers["x-api-key"] == "upstream-SENTINEL"
        assert "gateway-SENTINEL" not in str(req.headers)
        assert req.headers["anthropic-beta"] == "fixture-beta"
        assert req.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(200, json=REPLY)

    receipts: list[ModelClaudeGatewayReceipt] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application(respond, receipts)),
        base_url="http://localhost",
    ) as client:
        response = await client.post(
            "/v1/messages",
            json=full,
            headers={
                **AUTH,
                "anthropic-beta": "fixture-beta",
                "anthropic-version": "2023-06-01",
            },
        )
    assert response.status_code == 200
    assert response.json() == REPLY
    assert len(seen) == len(receipts) == 1
    receipt = receipts[0]
    assert response.headers["x-onex-run-id"] == str(receipt.run_id)
    assert receipt.status == "completed"
    assert (receipt.in_tokens, receipt.out_tokens, receipt.cache_read_tokens) == (
        3,
        5,
        2,
    )
    assert receipt.cost_usd is None
    assert all(
        word not in receipt.model_dump_json()
        for word in ("SENTINEL", "tool_result", "system")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_sse_preserves_bytes_and_requires_message_stop(complete: bool) -> None:
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_fixture",
                "model": "claude-fixture",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 2,
                },
            },
        },
        {"type": "ping"},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "t1",
                "name": "Read",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 5},
        },
    ]
    if complete:
        events.append({"type": "message_stop"})
    wire = b"".join(
        (f"event: {event['type']}\ndata: {json.dumps(event)}\n\n").encode()
        for event in events
    )

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for position in range(0, len(wire), 13):
                yield wire[position : position + 13]

    receipts: list[ModelClaudeGatewayReceipt] = []
    app = application(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Chunks()
        ),
        receipts,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/v1/messages", json={**BODY, "stream": True}, headers=AUTH
        )
    assert response.status_code == 200
    assert response.content.startswith(wire)
    assert (response.content == wire) is complete
    assert len(receipts) == 1
    assert receipts[0].status == ("completed" if complete else "failed")
    assert (receipts[0].in_tokens, receipts[0].out_tokens) == (3, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "redirect", "outage", "receipt"])
async def test_failure_does_not_bypass_or_leak(failure: str) -> None:
    seen: list[str] = []

    def respond(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if failure == "outage":
            raise httpx.ConnectError("SENTINEL-private", request=req)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://bypass.example"})
        if failure == "error":
            return httpx.Response(500, text="SENTINEL-private")
        return httpx.Response(200, json=REPLY)

    app = application(respond, [], fail_record=failure == "receipt")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        response = await client.post("/v1/messages", json=BODY, headers=AUTH)
    assert response.status_code == 502
    assert "SENTINEL" not in response.text
    assert seen == ["https://upstream.example/v1/messages"]


@pytest.mark.asyncio
async def test_unknown_model_refused_and_count_tokens_is_not_inference() -> None:
    seen: list[str] = []

    def respond(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(200, json={"input_tokens": 3})

    receipts: list[ModelClaudeGatewayReceipt] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application(respond, receipts)),
        base_url="http://localhost",
    ) as client:
        assert (
            await client.post(
                "/v1/messages", json={**BODY, "model": "other"}, headers=AUTH
            )
        ).status_code == 400
        assert (
            await client.post("/v1/messages/count_tokens", json=BODY, headers=AUTH)
        ).json() == {"input_tokens": 3}
    assert seen == ["/v1/messages/count_tokens"]
    assert receipts == []


def test_configuration_refuses_remote_bind() -> None:
    with pytest.raises(ValidationError):
        config(bind_host="0.0.0.0")


@pytest.mark.asyncio
async def test_incompatible_route_refused_before_inference() -> None:
    seen: list[str] = []

    async def read_secret(_: str) -> str:
        return "gateway-SENTINEL"

    async def record(_: ModelClaudeGatewayReceipt) -> None:
        pass

    app = build_gateway_app(
        config(),
        resolve_backend=lambda: backend("https://upstream.example/v1/chat/completions"),
        read_secret=read_secret,
        record=record,
        transport=httpx.MockTransport(
            lambda req: seen.append(str(req.url)) or httpx.Response(200)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        assert (
            await client.post("/v1/messages", json=BODY, headers=AUTH)
        ).status_code == 503
    assert seen == []
