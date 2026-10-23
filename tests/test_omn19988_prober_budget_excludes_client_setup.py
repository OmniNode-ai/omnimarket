# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19988: the probe budget covers the network exchange, not client setup.

A running service must not read DOWN because the HTTP client was slow to build
(lazy imports, TLS context) on a loaded host.
"""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from omnimarket.nodes.node_local_environment_observe_effect.handlers import (
    handler_local_environment_observe as observer,
)

BUDGET = 0.5
SETUP_DELAY = 1.0  # longer than the whole budget


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _SlowBuildClient(httpx.AsyncClient):
    """Client whose construction alone outlasts the probe budget."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        time.sleep(SETUP_DELAY)
        super().__init__(*args, **kwargs)


def _target(port: int) -> observer.ModelProbeTarget:
    return observer.ModelProbeTarget(
        service="127.0.0.1", port=port, scheme="http", path="/health"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slow_client_construction_does_not_spend_the_probe_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(observer.httpx, "AsyncClient", _SlowBuildClient)
    try:
        result = await observer.RealNetworkProber().probe(
            _target(server.server_port), BUDGET
        )
        assert result.observed_state.value == "running", result.reason
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_server_that_never_answers_still_reads_down_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()  # accepts connections in the backlog, never replies
    monkeypatch.setattr(observer.httpx, "AsyncClient", _SlowBuildClient)
    try:
        started = time.monotonic()
        result = await observer.RealNetworkProber().probe(
            _target(silent.getsockname()[1]), BUDGET
        )
        elapsed = time.monotonic() - started
        assert result.observed_state.value == "down", result.reason
        # setup delay is outside the window; the exchange is bounded by the budget
        assert elapsed < SETUP_DELAY + BUDGET + 0.5
    finally:
        silent.close()
