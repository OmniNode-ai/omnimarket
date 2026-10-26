# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The served-model guard holds on the runtime's own entrypoint (OMN-17098).

``test_omn17098_inference_intent_served_model_guard.py`` drives
``HandlerInferenceIntent.handle``. The runtime's handler wiring dispatches the
orchestrator's inference intent through ``handle_async`` instead. This module
drives that entrypoint against a loopback endpoint that serves a different
model from the one the intent names, and checks that no chat request is made
and that the response carries no attribution to the configured model.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

_CONFIGURED_MODEL = "model-a-configured"
_SERVED_MODEL = "model-b-actually-served"


@pytest.fixture(autouse=True)
def _fresh_served_models_state() -> Iterator[None]:
    # Read through getattr so the test fails on the attribution, not on setup,
    # when run against a tree without the served-models cache.
    cache = getattr(transport, "served_models_cache", {})
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def endpoint() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """A loopback endpoint listing only the served model; it records chat POSTs."""
    chat_posts: list[dict[str, Any]] = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(self, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            self._send({"data": [{"id": _SERVED_MODEL}]})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            chat_posts.append(json.loads(self.rfile.read(length) or b"{}"))
            self._send(
                {
                    "model": chat_posts[-1].get("model"),
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host!s}:{port}/v1/chat/completions", chat_posts
    finally:
        server.shutdown()
        server.server_close()


def test_runtime_entrypoint_refuses_a_model_the_endpoint_does_not_serve(
    endpoint: tuple[str, list[dict[str, Any]]],
) -> None:
    chat_url, chat_posts = endpoint
    intent = ModelInferenceIntent(
        base_url=chat_url,
        model=_CONFIGURED_MODEL,
        system_prompt="You are a test author.",
        prompt="Write a test.",
        max_tokens=64,
        temperature=0.0,
        timeout_seconds=10.0,
        correlation_id=uuid4(),
        inference_attempt_id=uuid4(),
    )

    response = asyncio.run(HandlerInferenceIntent().handle_async(intent))

    assert response.model_used != _CONFIGURED_MODEL
    assert response.content == ""
    assert response.error_message
    assert "model_attribution_mismatch" in response.error_message
    assert chat_posts == []
