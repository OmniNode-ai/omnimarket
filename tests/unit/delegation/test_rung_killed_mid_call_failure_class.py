# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21313: a rung killed mid-call is typed ``model_unavailable``, not ``unknown``.

THE DEFECT, found 2026-10-23 on omnimarket dev f9bc19a9105a, eval
rung-killed-or-slowed, run cf7e176d-9213-42c2-ac8d-610571ace8bd: a stand-in
rung for local-heavy-reasoning was killed with ``kill -9`` while its chat call
was open. The attempt was recorded ``failure_class=unknown`` with the error
"Server disconnected without sending a response." because the handler typed
only ``httpx.TimeoutException`` and ``httpx.HTTPStatusError`` ahead of its bare
``except Exception``.

The first two tests drive the real ``HandlerLlmDelegationCall.handle`` against
a live local server that accepts the chat POST and then closes the socket, once
cleanly (FIN) and once with a connection reset (RST). The receipt test replays
the recorded error text through the transport boundary. The control test shows
the stub answers a healthy call, so the kill is what the class reacts to.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    HandlerLlmDelegationCall,
)

pytestmark = pytest.mark.unit

_MODEL_ID = "omn21313-stub-model"
_RECEIPT_ERROR = "Server disconnected without sending a response."


def _make_provider(mode: str) -> type[BaseHTTPRequestHandler]:
    class _Provider(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return

        def _respond(self, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._respond({"data": [{"id": _MODEL_ID}]})

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if mode == "answer":
                self._respond(
                    {
                        "id": "x",
                        "model": _MODEL_ID,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "hi"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                )
                return
            self.close_connection = True
            if mode == "reset":
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
            else:
                self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

    return _Provider


def _serve(mode: str) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _make_provider(mode))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(params=["close", "reset"])
def killed_provider(request: pytest.FixtureRequest) -> Iterator[str]:
    yield from _serve(request.param)


@pytest.fixture
def answering_provider() -> Iterator[str]:
    yield from _serve("answer")


def _request(endpoint: str) -> ModelLlmDelegationCallRequest:
    return ModelLlmDelegationCallRequest(
        request_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        causation_id=str(uuid.uuid4()),
        model_id=_MODEL_ID,
        endpoint_ref=endpoint,
        prompt="say hi",
        prompt_hash="0" * 64,
        timeout_seconds=15.0,
        secret_ref=None,
        api_key_env=None,
    )


def test_server_closing_the_socket_mid_call_is_model_unavailable(
    killed_provider: str,
) -> None:
    result = HandlerLlmDelegationCall().handle(_request(killed_provider))

    assert result.success is False
    assert result.failure_class is EnumDelegationFailureClass.MODEL_UNAVAILABLE, (
        "a connection closed after the request was sent fell through to the "
        "bare `except Exception` and classified as UNKNOWN"
    )
    assert result.error_message, "the error text must be kept"


def test_control_the_stub_answers_when_it_is_not_killed(
    answering_provider: str,
) -> None:
    """Positive control: the kill, not the stub, produces the failure above."""
    result = HandlerLlmDelegationCall().handle(_request(answering_provider))

    assert result.success is True, result.error_message


def test_receipt_cf7e176d_server_disconnected_is_model_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded receipt's attempt error, replayed at the transport boundary."""

    def _disconnect(**_: object) -> object:
        raise httpx.RemoteProtocolError(_RECEIPT_ERROR)

    monkeypatch.setattr(
        transport, "get_served_model_ids", lambda *_a, **_k: {_MODEL_ID}
    )
    monkeypatch.setattr(transport, "probe_health", lambda *_a, **_k: True)
    monkeypatch.setattr(transport, "post_chat_completion", _disconnect)

    result = HandlerLlmDelegationCall().handle(
        _request("http://127.0.0.1:18735/v1/chat/completions")
    )

    assert result.success is False
    assert result.failure_class is EnumDelegationFailureClass.MODEL_UNAVAILABLE
    assert result.error_message == _RECEIPT_ERROR
