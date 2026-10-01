# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20299: a self-hosted model call carries the run's correlation id.

The model servers' own logs are the check no caller can skip: a request a
server answered either joins a ``delegation_events`` run or it bypassed the
delegation nodes. The .201 vLLM access log records each request's path and
query string, so the id rides in the query string (and in a header for any
log that records headers). The POST URL argument stays ``intent.base_url``
verbatim (OMN-12815); the query parameter is the httpx client's ``params``.
A third-party provider gets neither.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.models.model_call_correlation import (
    SELF_HOSTED_CORRELATION_HEADER,
    SELF_HOSTED_CORRELATION_QUERY_PARAM,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
    _is_self_hosted_endpoint,
)

pytestmark = pytest.mark.usefixtures("stub_provider_quota_reader")

_RESPONSE = {
    "id": "chatcmpl-omn20299",
    "choices": [{"message": {"content": "ok"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


class _RecordingHandler(BaseHTTPRequestHandler):
    seen: list[tuple[str, dict[str, str]]]

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.seen.append((self.path, dict(self.headers.items())))
        body = json.dumps(_RESPONSE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.mark.unit
def test_self_hosted_call_carries_the_correlation_id() -> None:
    """A real loopback server sees the id in the query string and the header."""
    seen: list[tuple[str, dict[str, str]]] = []
    handler_cls = type("_Handler", (_RecordingHandler,), {"seen": seen})
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        intent = ModelInferenceIntent(
            base_url=base_url,
            model="Qwen3.8-27B",
            system_prompt="s",
            prompt="p",
            max_tokens=8,
            timeout_seconds=30.0,
            correlation_id=uuid4(),
        )
        HandlerInferenceIntent().handle(intent)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert len(seen) == 1
    path, headers = seen[0]
    parsed = urlparse(path)
    assert parsed.path == "/v1/chat/completions"
    assert parse_qs(parsed.query) == {
        SELF_HOSTED_CORRELATION_QUERY_PARAM: [str(intent.correlation_id)]
    }
    assert headers[SELF_HOSTED_CORRELATION_HEADER] == str(intent.correlation_id)


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url",
    [
        "http://10.0.0.201:8000/v1/chat/completions",
        "http://127.0.0.1:8000/v1/chat/completions",
        "http://localhost:8130/v1/chat/completions",
    ],
)
def test_lab_and_local_endpoints_are_self_hosted(base_url: str) -> None:
    assert _is_self_hosted_endpoint(base_url)


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url",
    [
        "https://openrouter.ai/api/v1/chat/completions",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "http://8.8.8.8:8000/v1/chat/completions",
    ],
)
def test_third_party_endpoints_are_not_self_hosted(base_url: str) -> None:
    assert not _is_self_hosted_endpoint(base_url)


@pytest.mark.unit
def test_query_parameter_name_is_the_one_the_reconciler_reads() -> None:
    """omnibase_internal's model-server reconciler parses exactly this name."""
    assert SELF_HOSTED_CORRELATION_QUERY_PARAM == "onex_cid"
    assert SELF_HOSTED_CORRELATION_HEADER == "X-Onex-Correlation-Id"
