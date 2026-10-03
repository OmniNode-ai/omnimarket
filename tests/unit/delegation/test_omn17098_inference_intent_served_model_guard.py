# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerInferenceIntent refuses to attribute a call to a model nobody serves.

The orchestrator's dispatch path executes through ``HandlerInferenceIntent``, and
that handler used to stamp ``model_used=intent.model`` unconditionally. A route
whose configured model id is not the one the endpoint has loaded (a retired
model, a stale overlay row) therefore recorded a false attribution on every
event downstream. The sibling ``HandlerLlmDelegationCall`` already refuses that
call with the typed ``model_attribution_mismatch``; these tests pin the same
refusal on the inference-intent path, against a real OpenAI-compatible server
bound to loopback so the whole wire (models listing, then the chat POST) is
exercised.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _inference_error_failure_class,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

_CONFIGURED_MODEL = "model-a-configured"
_SERVED_MODEL = "model-b-actually-served"


class _StubOpenAiServer:
    """A loopback OpenAI-compatible endpoint that records the chat POSTs it gets."""

    def __init__(self, models_status: int, served_ids: list[str]) -> None:
        self.chat_posts: list[dict[str, Any]] = []
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if self.path.split("?")[0] == "/v1/models" and models_status == 200:
                    self._send(
                        200,
                        {"data": [{"id": model_id} for model_id in served_ids]},
                    )
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                stub.chat_posts.append(json.loads(self.rfile.read(length) or b"{}"))
                # Echo the REQUESTED model back whatever is loaded, the way SGLang
                # does: the response body cannot expose a mismatch.
                self._send(
                    200,
                    {
                        "id": "chatcmpl-stub",
                        "model": stub.chat_posts[-1].get("model"),
                        "choices": [
                            {
                                "message": {"content": "def test_x(): pass"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 3,
                            "completion_tokens": 4,
                            "total_tokens": 7,
                        },
                    },
                )

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def chat_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/v1/chat/completions"

    def __enter__(self) -> _StubOpenAiServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture(autouse=True)
def _fresh_served_models_state() -> Iterator[None]:
    transport.served_models_cache.clear()
    yield
    transport.served_models_cache.clear()


def _intent(chat_url: str, model: str) -> ModelInferenceIntent:
    return ModelInferenceIntent(
        base_url=chat_url,
        model=model,
        system_prompt="You are a test author.",
        prompt="Write a test.",
        max_tokens=256,
        temperature=0.0,
        timeout_seconds=10.0,
        correlation_id=uuid4(),
        inference_attempt_id=uuid4(),
    )


def test_configured_model_not_served_is_refused_not_attributed() -> None:
    """Endpoint serves B, intent says A: no call, no A attribution, typed mismatch."""
    with _StubOpenAiServer(200, [_SERVED_MODEL]) as server:
        intent = _intent(server.chat_url, _CONFIGURED_MODEL)

        response = HandlerInferenceIntent().handle(intent)

    assert response.model_used != _CONFIGURED_MODEL
    assert response.content == ""
    assert response.error_message
    assert "model_attribution_mismatch" in response.error_message
    assert _CONFIGURED_MODEL in response.error_message
    assert _SERVED_MODEL in response.error_message
    # The refusal lands before the network: nothing was generated by anyone.
    assert server.chat_posts == []
    # The orchestrator reads the same typed class the sibling handler raises.
    assert (
        _inference_error_failure_class(response.error_message)
        is EnumDelegationFailureClass.MODEL_ATTRIBUTION_MISMATCH
    )
    # The attempt id still round-trips so the orchestrator binds the refusal to
    # the live route instead of rejecting it as a stale response.
    assert response.inference_attempt_id == intent.inference_attempt_id


def test_configured_model_served_is_still_recorded() -> None:
    """Control: the configured model IS served, so it is the recorded model."""
    with _StubOpenAiServer(200, [_SERVED_MODEL, _CONFIGURED_MODEL]) as server:
        intent = _intent(server.chat_url, _CONFIGURED_MODEL)

        response = HandlerInferenceIntent().handle(intent)

    assert not response.error_message
    assert response.model_used == _CONFIGURED_MODEL
    assert response.content == "def test_x(): pass"
    assert [post["model"] for post in server.chat_posts] == [_CONFIGURED_MODEL]


def test_endpoint_without_a_models_listing_is_no_evidence_not_a_mismatch() -> None:
    """A backend that exposes no ``/v1/models`` keeps the pre-guard behaviour."""
    with _StubOpenAiServer(404, []) as server:
        intent = _intent(server.chat_url, _CONFIGURED_MODEL)

        response = HandlerInferenceIntent().handle(intent)

    assert not response.error_message
    assert response.model_used == _CONFIGURED_MODEL
    assert len(server.chat_posts) == 1
