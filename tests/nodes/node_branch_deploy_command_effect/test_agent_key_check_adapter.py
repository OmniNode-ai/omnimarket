# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent key check http adapter of node_branch_deploy_command_effect (OMN-24396)."""

from __future__ import annotations

import http.server
import json
import socket
import threading
from typing import Any

import pytest

from omnimarket.nodes.node_branch_deploy_command_effect.adapters import (
    adapter_agent_key_check_http,
)
from omnimarket.nodes.node_branch_deploy_command_effect.adapters.adapter_agent_key_check_http import (
    AgentKeyCheckHttpReader,
)

pytestmark = pytest.mark.unit

REAL_HTTP_BOUNDARY = True  # conftest.py leaves the http boundary alone for this module


class _Handler(http.server.BaseHTTPRequestHandler):
    status_code: int = 200
    body: bytes = b"{}"

    def do_GET(self) -> None:
        self.send_response(self.status_code)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: Any) -> None:
        return


def _start_server(
    status_code: int, body: bytes
) -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = type("Handler", (_Handler,), {"status_code": status_code, "body": body})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _reader(url: str, field: str = "hmac_key_check") -> AgentKeyCheckHttpReader:
    return AgentKeyCheckHttpReader(url, 5.0, field)


def test_read_returns_the_key_check_value() -> None:
    body = json.dumps({"hmac_key_check": "0123456789abcdef"}).encode()
    server, port = _start_server(200, body)
    try:
        reading = _reader(f"http://127.0.0.1:{port}").read()
        assert reading.status == "read"
        assert reading.key_check == "0123456789abcdef"
    finally:
        server.shutdown()


def test_read_returns_the_key_check_value_on_http_503() -> None:
    body = json.dumps({"hmac_key_check": "0123456789abcdef"}).encode()
    server, port = _start_server(503, body)
    try:
        reading = _reader(f"http://127.0.0.1:{port}").read()
        assert reading.status == "read"
        assert reading.key_check == "0123456789abcdef"
    finally:
        server.shutdown()


def test_read_returns_absent_when_the_field_is_missing() -> None:
    body = json.dumps({}).encode()
    server, port = _start_server(200, body)
    try:
        reading = _reader(f"http://127.0.0.1:{port}").read()
        assert reading.status == "absent"
    finally:
        server.shutdown()


def test_read_returns_absent_when_the_field_is_null() -> None:
    body = json.dumps({"hmac_key_check": None}).encode()
    server, port = _start_server(200, body)
    try:
        reading = _reader(f"http://127.0.0.1:{port}").read()
        assert reading.status == "absent"
    finally:
        server.shutdown()


def test_read_returns_unreachable_when_the_body_is_not_json() -> None:
    server, port = _start_server(200, b"not json")
    try:
        reading = _reader(f"http://127.0.0.1:{port}").read()
        assert reading.status == "unreachable"
        assert reading.detail == "JSONDecodeError"
    finally:
        server.shutdown()


def test_read_returns_unreachable_when_the_port_is_closed() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    reading = _reader(f"http://127.0.0.1:{port}").read()
    assert reading.status == "unreachable"


def test_read_returns_unreachable_when_the_module_fetch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(url: str, timeout_seconds: float) -> bytes:
        raise TimeoutError("timed out")

    monkeypatch.setattr(adapter_agent_key_check_http, "fetch_health", _raise)
    reader = AgentKeyCheckHttpReader("http://127.0.0.1:1", 5.0, "hmac_key_check")
    reading = reader.read()
    assert reading.status == "unreachable"
    assert reading.detail == "TimeoutError"
