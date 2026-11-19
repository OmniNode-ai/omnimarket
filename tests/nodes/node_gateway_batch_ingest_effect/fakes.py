# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the batch ingest door tests: overlay, lab key and tokens, a recording publisher, an ASGI harness."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
import yaml
from cryptography.hazmat.primitives.asymmetric import ec

from omnimarket.nodes.node_gateway_batch_ingest_effect.asgi import (
    BatchIngestApp,
    OverlayBindings,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.claims_jwks import LabJwksClaims
from omnimarket.nodes.node_gateway_batch_ingest_effect.event_identity import (
    derive_event_id,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelPublishOutcome,
    ModelWireRecord,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
)

PREFIX = "/v1/gateway/batch-ingest"
MOUNT_CEILING = 65536
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"
ISSUER = "https://keycloak.example.invalid/realms/lab"
# The two canonical topics are the ones the node's own contract declares; the tests read them from there.
_DECLARED_TOPICS = yaml.safe_load(
    (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_gateway_batch_ingest_effect/contract.yaml"
    ).read_text(encoding="utf-8")
)["event_bus"]["publish_topics"]
TOPIC_A: str = _DECLARED_TOPICS[0]
TOPIC_B: str = _DECLARED_TOPICS[1]
KID = "lab-test-key"

_PRIVATE = ec.generate_private_key(ec.SECP256R1())
_OTHER = ec.generate_private_key(ec.SECP256R1())


def _public_jwk(private: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, str]:
    data = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private.public_key()))
    return {**data, "kid": kid}


def overlay_document(**limits: object) -> dict[str, Any]:
    return {
        "claims": {
            "verifier": "lab-jwks",
            "issuer": ISSUER,
            "audience": "gateway-attach",
            "algorithms": ["ES256"],
            "max_lifetime_seconds": 900,
            "jwks_keys": [_public_jwk(_PRIVATE, KID)],
        },
        "tenants": [
            {"tenant_id": T1, "tenant_slug": "a", "status": "active"},
            {"tenant_id": T2, "tenant_slug": "b", "status": "active"},
        ],
        "topics": [
            {"topic": TOPIC_A, "topic_max_message_bytes": 4096},
            {"topic": TOPIC_B, "topic_max_message_bytes": 2048},
        ],
        "proxy_headers": [
            {"name": "x-forwarded-for", "max_value_bytes": 64},
            {"name": "x-request-id", "max_value_bytes": 32},
        ],
        "limits": {
            "max_batch_records": 100,
            "max_body_bytes": 16384,
            "chunk_headroom_bytes": MOUNT_CEILING + 1,
            "max_header_count": 16,
            "max_header_bytes": 512,
            "max_authorization_bytes": 1024,
            "max_json_depth": 8,
            "read_idle_timeout_s": 0.2,
            "total_upload_deadline_s": 0.6,
            "max_inflight_requests": 2,
            "max_inflight_bytes": 4 * (16384 + MOUNT_CEILING + 1),
            "max_topic_chars": 100,
            "max_wire_topic_chars": 140,
            "max_key_chars": 64,
            "max_value_chars": 8192,
            "max_record_headers": 4,
            "max_header_name_chars": 32,
            "max_header_value_chars": 64,
            "max_egress_value_bytes": 4000,
            **limits,
        },
        "kafka": {
            "lane": "dev",
            "producer_max_request_bytes": 4096,
            "message_timeout_ms": 1000,
            "record_overhead_bytes": 20,
            "header_overhead_bytes": 8,
        },
    }


def make_overlay(**limits: object) -> ModelBatchIngestOverlay:
    return ModelBatchIngestOverlay.model_validate(overlay_document(**limits))


def mint(
    *,
    tenant_id: str = T1,
    slug: str = "a",
    key: ec.EllipticCurvePrivateKey | None = None,
    audience: object = "gateway-attach",
    issuer: str = ISSUER,
    exp_in: int = 600,
    kid: str = KID,
) -> str:
    now = int(time.time())
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": "client-1",
        "iat": now,
        "exp": now + exp_in,
        "tenant_id": tenant_id,
        "tenant_slug": slug,
    }
    return jwt.encode(payload, key or _PRIVATE, algorithm="ES256", headers={"kid": kid})


def other_key() -> ec.EllipticCurvePrivateKey:
    return _OTHER


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def record(
    topic: str = TOPIC_A,
    value: bytes = b'{"n":1}',
    *,
    key: bytes | None = None,
    headers: dict[str, str] | None = None,
    wire_topic: str | None = None,
    event_id: str | None = None,
) -> dict[str, object]:
    lowered = {k.lower(): v.encode() for k, v in (headers or {}).items()}
    out: dict[str, object] = {
        "topic": wire_topic or topic,
        "key": b64(key) if key is not None else None,
        "value": b64(value),
        "event_id": event_id or derive_event_id(topic, key, lowered, value),
        "envelope_id": "env-1",
    }
    if headers:
        out["headers"] = headers
    return out


def batch(*records: dict[str, object]) -> bytes:
    return json.dumps({"records": list(records)}).encode()


@dataclass
class FakePublisher:
    """Records every call; ``script`` maps a call number to the outcome of that call (default published)."""

    message_max_bytes: int = 4096
    script: dict[int, str] = field(default_factory=dict)
    calls: list[ModelWireRecord] = field(default_factory=list)

    async def publish(self, record: ModelWireRecord) -> ModelPublishOutcome:
        number = len(self.calls)
        self.calls.append(record)
        status = self.script.get(number, "published")
        return ModelPublishOutcome(status=status)  # type: ignore[arg-type]


def make_app(
    overlay: ModelBatchIngestOverlay | None = None,
    publisher: FakePublisher | None = None,
) -> tuple[BatchIngestApp, FakePublisher]:
    loaded = overlay or make_overlay()
    fake = publisher or FakePublisher(
        message_max_bytes=loaded.kafka.producer_max_request_bytes
    )
    app = BatchIngestApp(
        overlay=loaded,
        claims=LabJwksClaims(loaded.claims),
        bindings=OverlayBindings(loaded),
        publisher=fake,
        path_prefix=PREFIX,
        mount_max_body_bytes=MOUNT_CEILING,
    )
    return app, fake


@dataclass
class Result:
    status: int
    body: dict[str, Any]
    receive_calls: int
    chunk_sizes: list[int]


async def post(
    app: BatchIngestApp,
    body: bytes = b"",
    *,
    token: str | None = None,
    chunks: list[bytes] | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
    content_length: bytes | None | str = "auto",
    stall: bool = False,
    disconnect: bool = False,
    receive_hook: Callable[[], Awaitable[None]] | None = None,
    path: str = PREFIX,
    method: str = "POST",
) -> Result:
    pending = list(chunks) if chunks is not None else [body]
    state = {"calls": 0}
    sizes: list[int] = []

    async def receive() -> dict[str, Any]:
        state["calls"] += 1
        if receive_hook is not None:
            await receive_hook()
        if disconnect:
            return {"type": "http.disconnect"}
        if pending:
            chunk = pending.pop(0)
            sizes.append(len(chunk))
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": bool(pending) or stall,
            }
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    total = sum(len(c) for c in (chunks if chunks is not None else [body]))
    raw: list[tuple[bytes, bytes]] = [
        (b"host", b"door"),
        (b"content-type", b"application/json"),
    ]
    if content_length == "auto":
        raw.append((b"content-length", str(total).encode()))
    elif isinstance(content_length, bytes):
        raw.append((b"content-length", content_length))
    raw.append((b"authorization", b"Bearer " + (token or mint()).encode()))
    raw.extend(headers or [])
    scope = {"type": "http", "method": method, "path": path, "headers": raw}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return Result(start["status"], json.loads(payload), state["calls"], sizes)


STAMPS_BYTES = 209  # message-id 82 + onex-tenant-id 58 + onex-tenant-slug 25 + onex-ingest-door 44, for slug "a"
RECORD_OVERHEAD = 20
