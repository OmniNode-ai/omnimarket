# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The mounted ASGI app: a thin adapter from HTTP to the handler. It owns no business rule.

Order of refusal, each before any later work: method and path, header limits (431), content-length (400, 413), the
in-flight reservation (503), the bearer token (401), then the bounded body read (408, 413), the JSON read (422, 400) and
the handler. The reservation is released in a finally on every exit.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_gateway_batch_ingest_effect.claims_jwks import LabJwksClaims
from omnimarket.nodes.node_gateway_batch_ingest_effect.handlers.handler_gateway_batch_ingest import (
    HandlerGatewayBatchIngest,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.ingress import (
    InflightGate,
    IngressRefusedError,
    check_headers,
    declared_length,
    reservation_bytes,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.json_guard import (
    JsonRefusedError,
    parse_json,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.kafka_publisher import (
    KafkaConfirmingPublisher,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelBatchIngestRequest,
    ModelTenantBinding,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
    load_overlay,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.protocols.protocol_record_publisher import (
    ProtocolRecordPublisher,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.protocols.protocol_tenant_bindings import (
    ProtocolTenantBindings,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.protocols.protocol_tenant_claims import (
    ClaimsRefusedError,
    ProtocolTenantClaims,
)

_Scope = MutableMapping[str, Any]
_Message = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]

CONTRACT_PATH = Path(__file__).with_name("contract.yaml")


def mount_ceiling() -> tuple[str, int]:
    """The contract's mount path prefix and the body ceiling the runtime enforces on it."""
    mount = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))["http_mount"]
    return str(mount["path_prefix"]), int(mount["max_body_bytes"])


class OverlayBindings:
    """The tenant registry the overlay declares."""

    def __init__(self, overlay: ModelBatchIngestOverlay) -> None:
        self._by_id = {t.tenant_id: t for t in overlay.tenants}

    def binding_for(self, tenant_id: str) -> ModelTenantBinding | None:
        return self._by_id.get(tenant_id)


class BatchIngestApp:
    """One ASGI application; its only mutable state is the in-flight counters."""

    def __init__(
        self,
        *,
        overlay: ModelBatchIngestOverlay,
        claims: ProtocolTenantClaims,
        bindings: ProtocolTenantBindings,
        publisher: ProtocolRecordPublisher | None,
        path_prefix: str,
        mount_max_body_bytes: int,
    ) -> None:
        if overlay.limits.max_body_bytes > mount_max_body_bytes:
            raise ValueError(
                "overlay max_body_bytes exceeds the contract's http_mount max_body_bytes"
            )
        if overlay.limits.chunk_headroom_bytes <= mount_max_body_bytes:
            raise ValueError(
                "chunk_headroom_bytes must exceed the mount's max_body_bytes so no server chunk can pass its reservation"
            )
        self._overlay = overlay
        self._claims = claims
        self._bindings = bindings
        self._publisher = publisher
        self._path_prefix = path_prefix
        self._gate = InflightGate(
            overlay.limits.max_inflight_requests, overlay.limits.max_inflight_bytes
        )
        self._handler: HandlerGatewayBatchIngest | None = None
        if publisher is not None:
            self._handler = HandlerGatewayBatchIngest(
                overlay=overlay, bindings=bindings, publisher=publisher
            )

    @property
    def gate(self) -> InflightGate:
        return self._gate

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "http":
            await self._http(scope, receive, send)

    async def _lifespan(self, receive: _Receive, send: _Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    if self._publisher is None:
                        self._publisher = KafkaConfirmingPublisher.from_overlay(
                            self._overlay, os.environ
                        )
                    self._handler = HandlerGatewayBatchIngest(
                        overlay=self._overlay,
                        bindings=self._bindings,
                        publisher=self._publisher,
                    )
                except Exception as exc:
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                close = getattr(self._publisher, "close", None)
                if callable(close):
                    close()
                await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    async def _respond(
        send: _Send, status: int, body: dict[str, object], *, close: bool = False
    ) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ]
        if close:
            headers.append((b"connection", b"close"))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": payload})

    async def _http(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope["path"] != self._path_prefix:
            await self._respond(send, 404, {"error": "not-found"})
            return
        if scope["method"] != "POST":
            await self._respond(send, 405, {"error": "method-not-allowed"})
            return
        if self._handler is None:
            await self._respond(send, 503, {"error": "starting", "retryable": True})
            return
        reserved = 0
        try:
            headers = check_headers(list(scope["headers"]), self._overlay)
            declared = declared_length(headers, self._overlay)
            reserved = reservation_bytes(declared, self._overlay)
            if not self._gate.try_reserve(reserved):
                reserved = 0
                await self._respond(
                    send, 503, {"error": "inflight-exceeded", "retryable": True}
                )
                return
            token = self._bearer(headers)
            try:
                verified = self._claims.verify(token)
            except ClaimsRefusedError as exc:
                await self._respond(send, 401, {"error": exc.reason})
                return
            body = await self._read_body(receive, declared)
            try:
                document = parse_json(
                    body, max_depth=self._overlay.limits.max_json_depth
                )
            except JsonRefusedError as exc:
                await self._respond(
                    send,
                    422 if exc.reason == "json-too-deep" else 400,
                    {"error": exc.reason},
                )
                return
            response = await self._handler.handle(
                ModelBatchIngestRequest(claims=verified, document=document)
            )
            await self._respond(send, response.status_code, response.body)
        except IngressRefusedError as exc:
            await self._respond(
                send, exc.status, {"error": exc.reason}, close=exc.status in (408, 413)
            )
        finally:
            if reserved:
                self._gate.release(reserved)

    @staticmethod
    def _bearer(headers: dict[str, bytes]) -> str:
        raw = headers.get("authorization", b"").decode("latin-1")
        scheme, _, token = raw.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise IngressRefusedError(401, "authorization-missing")
        return token.strip()

    async def _read_body(self, receive: _Receive, declared: int | None) -> bytes:
        """Read at most the bound plus one chunk, under an idle timeout and a total upload deadline."""
        lim = self._overlay.limits
        bound = lim.max_body_bytes if declared is None else declared
        loop = asyncio.get_running_loop()
        deadline = loop.time() + lim.total_upload_deadline_s
        body = bytearray()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise IngressRefusedError(408, "body-timeout")
            try:
                message = await asyncio.wait_for(
                    receive(), min(lim.read_idle_timeout_s, remaining)
                )
            except TimeoutError:
                raise IngressRefusedError(408, "body-timeout") from None
            if message["type"] == "http.disconnect":
                raise IngressRefusedError(400, "client-disconnected")
            body.extend(message.get("body", b""))
            if len(body) > bound:
                raise IngressRefusedError(413, "body-too-large")
            if not message.get("more_body", False):
                return bytes(body)


def create_app(
    *,
    overlay: ModelBatchIngestOverlay | None = None,
    claims: ProtocolTenantClaims | None = None,
    bindings: ProtocolTenantBindings | None = None,
    publisher: ProtocolRecordPublisher | None = None,
    environ: dict[str, str] | None = None,
) -> BatchIngestApp:
    """The factory the contract's `http_mount` names; with no arguments everything comes from the overlay."""
    loaded = overlay or load_overlay(environ)
    prefix, ceiling = mount_ceiling()
    return BatchIngestApp(
        overlay=loaded,
        claims=claims or LabJwksClaims(loaded.claims),
        bindings=bindings or OverlayBindings(loaded),
        publisher=publisher,
        path_prefix=prefix,
        mount_max_body_bytes=ceiling,
    )


__all__ = ["BatchIngestApp", "OverlayBindings", "create_app", "mount_ceiling"]
