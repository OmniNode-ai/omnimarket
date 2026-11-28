# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live responder reader against real sockets: llama.cpp listing, a dead responder, least-busy order (OMN-26043)."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from omnimarket.handlers.routing.delegation_check_responder_chain import resolve_chain
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    HttpResponderReader,
)


def _serve(
    models: Mapping[str, object], slots: Sequence[Mapping[str, object]] | None
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/v1/models":
                payload: object = models
            elif self.path == "/slots" and slots is not None:
                payload = slots
            else:
                self.send_response(501)
                self.end_headers()
                return
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


LLAMACPP = {
    "data": [{"id": "Qwen3.8-27B", "meta": {"n_ctx": 131072, "n_ctx_train": 262144}}]
}


@pytest.fixture
def llamacpp_idle() -> Iterator[str]:
    yield from _serve(LLAMACPP, [{"id": 0, "is_processing": False}])


@pytest.fixture
def llamacpp_busy() -> Iterator[str]:
    yield from _serve(
        LLAMACPP, [{"id": 0, "is_processing": True}, {"id": 1, "is_processing": True}]
    )


@pytest.fixture
def dead_base() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _chain(*bases: str) -> tuple[ModelDelegationCheckResponder, ...]:
    return tuple(ModelDelegationCheckResponder(tier="local", base_url=b) for b in bases)


def test_a_llamacpp_responder_without_max_model_len_is_chosen(
    llamacpp_idle: str,
) -> None:
    got = resolve_chain(_chain(llamacpp_idle), HttpResponderReader(2.0).read)
    assert got.base_url == llamacpp_idle
    assert got.window == 131072


def test_a_dead_first_responder_does_not_stop_the_chain(
    dead_base: str, llamacpp_idle: str
) -> None:
    got = resolve_chain(_chain(dead_base, llamacpp_idle), HttpResponderReader(2.0).read)
    assert got.base_url == llamacpp_idle
    assert [a.outcome for a in got.attempts] == ["no-answer", "chosen"]


def test_every_responder_dead_advances_to_the_cloud_tier(dead_base: str) -> None:
    responders = (
        *_chain(dead_base),
        ModelDelegationCheckResponder(tier="openrouter"),
    )
    got = resolve_chain(responders, HttpResponderReader(2.0).read)
    assert got.tier == "openrouter"
    assert not got.is_local


def test_least_busy_wins_over_declared_order(
    llamacpp_busy: str, llamacpp_idle: str
) -> None:
    got = resolve_chain(
        _chain(llamacpp_busy, llamacpp_idle), HttpResponderReader(2.0).read
    )
    assert got.base_url == llamacpp_idle
