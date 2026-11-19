# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Recording doors for the HTTPS forwarder's request and receipt tests."""

from __future__ import annotations

import ipaddress
import json
import ssl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from socketserver import StreamRequestHandler, ThreadingTCPServer
from threading import Thread
from typing import Any

import httpx
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from omnimarket.nodes.node_runtime_bus_forward_effect.overlay import (
    ModelRuntimeBusForwardOverlay,
)

_PRODUCER_CONTRACT = files("omnimarket.nodes").joinpath(
    "node_pool_usage_verdict_compute", "contract.yaml"
)
TOPIC: str = yaml.safe_load(_PRODUCER_CONTRACT.read_text(encoding="utf-8"))[
    "terminal_event"
]
EVENT_TOPIC_PREFIX = ".".join(TOPIC.split(".", maxsplit=2)[:2]) + "."
ROUTE = "/v1/gateway/batch-ingest"
URL = "https://ingest.example.invalid" + ROUTE
URL_REF = "gateway.ingest.url"
AUTH_REF = "gateway.ingest.token"
TOKEN = "fixture-token"


def document(**bus: object) -> dict[str, Any]:
    return {
        "event_bus": {
            "type": "https",
            "ingest_url_ref": URL_REF,
            "auth_ref": AUTH_REF,
            "max_batch_records": 4,
            "max_event_bytes": 512,
            "max_batch_bytes": 4096,
            "max_authorization_bytes": 60,
            "max_key_bytes": 32,
            "max_value_bytes": 128,
            "max_record_headers": 6,
            "max_header_name_chars": 24,
            "max_header_value_chars": 32,
            "max_topic_chars": 80,
            "door_record_confirm_timeout_seconds": 1,
            "request_margin_seconds": 1,
            "request_timeout_seconds": 6,
            "max_attempts": 2,
            "retry_backoff_seconds": 2,
            **bus,
        },
        "max_events": 20,
        "flush_timeout_seconds": 600,
        "allowed_topic_prefixes": [EVENT_TOPIC_PREFIX],
    }


def overlay(**bus: object) -> ModelRuntimeBusForwardOverlay:
    return ModelRuntimeBusForwardOverlay.model_validate(document(**bus))


def records(body: bytes) -> list[dict[str, Any]]:
    return json.loads(body)["records"]


class RecordingDoor:
    def __init__(
        self,
        answer: Callable[[int, httpx.Request], httpx.Response] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.answer is not None:
            return self.answer(len(self.requests), request)
        return accepted(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def accepted(request: httpx.Request) -> httpx.Response:
    return httpx.Response(202, json={"published": len(records(request.content))})


def outcome(
    request: httpx.Request,
    published: int,
    *,
    status: int = 503,
    reason: str = "delivery-unconfirmed",
    retryable: bool = True,
) -> httpx.Response:
    indexed = [
        {"index": index, "event_id": record["event_id"]}
        for index, record in enumerate(records(request.content))
    ]
    return httpx.Response(
        status,
        json={
            "published": indexed[:published],
            "failed": [
                {**indexed[published], "reason": reason, "retryable": retryable}
            ],
            "not_attempted": indexed[published + 1 :],
        },
    )


def _tls_contexts(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    bundle = tmp_path / "loopback.pem"
    bundle.write_bytes(
        pem
        + private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server.load_cert_chain(bundle)
    client = ssl.create_default_context(cadata=pem.decode("ascii"))
    return server, client


def respond(handler: StreamRequestHandler, status: int, body: bytes) -> None:
    handler.wfile.write(
        f"HTTP/1.1 {status} test\r\nConnection: close\r\n"
        f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )


@contextmanager
def loopback_door(
    tmp_path: Path,
    answer: Callable[[int, StreamRequestHandler, bytes], None] | None = None,
) -> Iterator[tuple[str, ssl.SSLContext, list[tuple[bytes, bytes]]]]:
    server_context, client_context = _tls_contexts(tmp_path)
    requests: list[tuple[bytes, bytes]] = []

    class Door(StreamRequestHandler):
        def handle(self) -> None:
            request_line = self.rfile.readline()
            if not request_line:
                return
            headers: list[bytes] = []
            while (line := self.rfile.readline()) != b"\r\n":
                if not line:
                    return
                headers.append(line)
            head = request_line + b"".join(headers) + b"\r\n"
            length = next(
                int(line.split(b":", 1)[1])
                for line in headers
                if line.lower().startswith(b"content-length:")
            )
            body = self.rfile.read(length)
            requests.append((head, body))
            if answer is not None:
                answer(len(requests), self, body)
            else:
                respond(self, 202, b'{"published":1}')

    class Server(ThreadingTCPServer):
        daemon_threads = True

    with Server(("127.0.0.1", 0), Door) as server:
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        worker = Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        worker.start()
        try:
            yield (
                f"https://127.0.0.1:{server.server_address[1]}{ROUTE}",
                client_context,
                requests,
            )
        finally:
            server.shutdown()
            worker.join()
