# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC19 and AC18: the real forwarding client and a real HTTP client over a TLS loopback socket against the door.

The door runs under uvicorn on a free loopback port with a self-signed certificate; the sender is the merged
`PublisherHttps`, so what the door reads is what the client puts on the wire, not a hand-built ASGI scope.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from omnimarket.nodes.node_gateway_batch_ingest_effect.asgi import BatchIngestApp
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import CLIENT_HEADER_SET
from omnimarket.nodes.node_runtime_bus_forward_effect.publishers.publisher_https import (
    PublisherHttps,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    PREFIX,
    TOPIC_A,
    batch,
    make_app,
    make_overlay,
    mint,
    record,
)
from tests.unit.nodes.node_runtime_bus_forward_effect.https_test_door import (
    _tls_contexts,
)
from tests.unit.nodes.node_runtime_bus_forward_effect.https_test_door import (
    overlay as forwarder_overlay,
)

_DECLARED = len(CLIENT_HEADER_SET) + len(make_overlay().proxy_headers)


@contextmanager
def _loopback_door(app: BatchIngestApp, tmp_path: Path) -> Iterator[tuple[str, Any]]:
    _, client_context = _tls_contexts(tmp_path)
    bundle = str(tmp_path / "loopback.pem")
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        ssl_certfile=bundle,
        ssl_keyfile=bundle,
        lifespan="off",
        log_level="warning",
    )
    server = uvicorn.Server(config)
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the loopback door did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"https://127.0.0.1:{port}{PREFIX}", client_context
    finally:
        server.should_exit = True
        worker.join(timeout=10)


def _sender(url: str, verify: Any, token: str) -> PublisherHttps:
    config = forwarder_overlay(
        max_authorization_bytes=1024,
        max_event_bytes=2048,
        max_value_bytes=512,
        max_batch_bytes=8192,
    ).event_bus
    assert config.type == "https"
    return PublisherHttps(
        config,
        url=url,
        token=token,
        transport_factory=lambda: httpx.AsyncHTTPTransport(verify=verify),
    )


def test_header_budget_real_client_maximum_header_request_is_202(
    tmp_path: Path,
) -> None:
    app, publisher = make_app(make_overlay(max_header_count=_DECLARED))
    with _loopback_door(app, tmp_path) as (url, verify):
        sender = _sender(url, verify, mint())
        sender.publish(TOPIC_A, None, b'{"n":1}', {})
        assert sender.flush(10) == 0
    assert sender.failure_detail() is None
    assert [call.topic for call in publisher.calls] == ["tenant-a." + TOPIC_A]


def test_header_budget_real_client_allowlisted_proxy_headers_at_and_over_their_maximum(
    tmp_path: Path,
) -> None:
    app, publisher = make_app()
    body = batch(record())
    with _loopback_door(app, tmp_path) as (url, verify):
        base = {"authorization": f"Bearer {mint()}", "content-type": "application/json"}
        with httpx.Client(verify=verify) as client:
            at_maximum = client.post(
                url,
                content=body,
                headers={
                    **base,
                    "x-request-id": "r" * 32,
                    "x-forwarded-for": "f" * 64,
                },
            )
            over_request_id = client.post(
                url, content=body, headers={**base, "x-request-id": "r" * 33}
            )
            over_forwarded = client.post(
                url, content=body, headers={**base, "x-forwarded-for": "f" * 65}
            )
    assert at_maximum.status_code == 202
    assert (over_request_id.status_code, over_request_id.json()["error"]) == (
        431,
        "header-too-large",
    )
    assert (over_forwarded.status_code, over_forwarded.json()["error"]) == (
        431,
        "header-too-large",
    )
    assert len(publisher.calls) == 1
    assert not {"x-request-id", "x-forwarded-for"} & {
        name for name, _ in publisher.calls[0].headers
    }


def test_header_budget_real_client_undeclared_name_is_431_header_not_declared(
    tmp_path: Path,
) -> None:
    app, publisher = make_app()
    with (
        _loopback_door(app, tmp_path) as (url, verify),
        httpx.Client(verify=verify) as client,
    ):
        refused = client.post(
            url,
            content=batch(record()),
            headers={
                "authorization": f"Bearer {mint()}",
                "content-type": "application/json",
                "x-surprise": "1",
            },
        )
    assert (refused.status_code, refused.json()["error"]) == (
        431,
        "header-not-declared",
    )
    assert publisher.calls == []
