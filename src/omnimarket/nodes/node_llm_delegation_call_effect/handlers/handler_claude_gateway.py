# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Native Claude ingress in the model-call effect; routing stays with its authority.

Body bytes and native SSE events pass through. Only bounded usage metadata and
hashes survive the call; gateway credentials never become provider credentials.
There is no retry or alternate-provider path in this transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import JsonValue

from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.inference.provider_response_error import failure_class_for_status
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_claude_gateway import (
    ModelClaudeGatewayConfig,
    ModelClaudeGatewayReceipt,
)

_FORBIDDEN_HEADERS = frozenset(
    (
        "authorization",
        "x-api-key",
        "api-key",
        "proxy-authorization",
        "cookie",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
    )
)
_ERROR = {
    "type": "error",
    "error": {
        "type": "api_error",
        "message": "ONEX gateway request failed; inspect its run receipt. No direct-provider fallback was used.",
    },
}
_SSE_ERROR = ("event: error\ndata: " + json.dumps(_ERROR) + "\n\n").encode()
# Upstream answers that let the next native backend in the ladder take the request.
# Other client errors (400, 401, 403, 413, 422) fail visibly instead.
_FALLBACK_STATUS = frozenset((404, 408, 429))


def _fallback_eligible(status: int | None) -> bool:
    """None is a transport failure before any upstream response."""
    return status is None or status in _FALLBACK_STATUS or 500 <= status <= 599


def _json_object(raw: bytes) -> dict[str, JsonValue]:
    def reject_constant(_: str) -> None:
        raise ValueError("Non-finite JSON")

    value = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return cast("dict[str, JsonValue]", value)


def _count(value: JsonValue) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


class _Observation:
    """Bounded SSE parsing; raw bytes are forwarded separately, never reconstructed."""

    def __init__(self, *, model: str, limit: int) -> None:
        self.model = model
        self.limit = limit
        self.buffer = b""
        self.usage: dict[str, JsonValue] = {}
        self.started = False
        self.delta = False
        self.stopped = False
        self.stop_reason: str | None = None

    def read_body(self, body: dict[str, JsonValue]) -> None:
        if body.get("model") != self.model or not isinstance(body.get("content"), list):
            raise ValueError("Invalid native response")
        usage = body.get("usage")
        if isinstance(usage, dict):
            self.usage.update(usage)
        reason = body.get("stop_reason")
        if not isinstance(reason, str) or not reason:
            raise ValueError("Incomplete native response")
        self.stop_reason = reason

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while match := re.search(rb"\r\n\r\n|\n\n|\r\r", self.buffer):
            frame = self.buffer[: match.start()]
            self.buffer = self.buffer[match.end() :]
            if len(frame) > self.limit:
                raise ValueError("Native event too large")
            data = b"\n".join(
                line[5:].removeprefix(b" ")
                for line in frame.splitlines()
                if line.startswith(b"data:")
            )
            if not data:
                continue
            event = _json_object(data)
            kind = event.get("type")
            if kind == "error":
                raise ValueError("Native stream error")
            if kind == "message_start":
                message = event.get("message")
                if not isinstance(message, dict) or message.get("model") != self.model:
                    raise ValueError("Native model mismatch")
                self.started = True
                usage = message.get("usage")
                if isinstance(usage, dict):
                    self.usage.update(usage)
            elif kind == "message_delta":
                self.delta = True
                usage = event.get("usage")
                if isinstance(usage, dict):
                    self.usage.update(usage)
                delta = event.get("delta")
                reason = delta.get("stop_reason") if isinstance(delta, dict) else None
                if isinstance(reason, str):
                    self.stop_reason = reason
            elif kind == "message_stop":
                if not self.started or not self.delta:
                    raise ValueError("Incomplete native stream")
                self.stopped = True
        if len(self.buffer) > self.limit:
            raise ValueError("Native event too large")


async def _bounded_response(response: httpx.Response, limit: int) -> bytes:
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        raw.extend(chunk)
        if len(raw) > limit:
            raise ValueError("Native response too large")
    return bytes(raw)


def _endpoint(
    backend: ModelResolvedDelegationBackend, backend_id: str, model: str | None
) -> str:
    """The native Messages URI of one ladder entry; model is pinned for the primary."""
    endpoint = backend.endpoint_ref
    parts = urlsplit(endpoint)
    if backend.backend_id != backend_id or model not in (None, backend.model_id):
        raise ValueError("Routing selection mismatch")
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or not parts.path.endswith("/v1/messages")
    ):
        raise ValueError("Routing backend is not a native Claude endpoint")
    # This full URI comes only from the explicitly selected canonical backend,
    # never a request parameter. Local/LAN inference may use HTTP just as the
    # existing delegation transport does. Redirects remain disabled below.
    return endpoint


#: The upstream transport type, so callers need no transport import of their own.
GatewayTransport = httpx.AsyncBaseTransport


def build_gateway_app(
    config: ModelClaudeGatewayConfig,
    *,
    resolve_backend: Callable[[], ModelResolvedDelegationBackend],
    read_secret: Callable[[str], Awaitable[str | None]],
    record: Callable[[ModelClaudeGatewayReceipt], Awaitable[None]],
    transport: httpx.AsyncBaseTransport | None = None,
    resolve_fallbacks: Callable[[], Sequence[ModelResolvedDelegationBackend]] = tuple,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def authenticate(request: Request) -> None:
        try:
            expected = await read_secret(config.credential_ref)
            authorization = request.headers.get("authorization", "")
            supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
            if not authorization:
                supplied = request.headers.get("x-api-key", "")
            if (
                not expected
                or not supplied
                or not hmac.compare_digest(supplied.encode(), expected.encode())
            ):
                raise ValueError("Unauthenticated")
        except Exception:
            raise HTTPException(401, "Gateway authentication failed") from None

    async def routes(
        version: str, beta: str | None
    ) -> list[tuple[ModelResolvedDelegationBackend, str, dict[str, str]]]:
        """The usable native ladder, primary first. An entry that cannot serve native
        Messages, or whose credential is absent, is left out and never called."""
        try:
            offered = {item.backend_id: item for item in resolve_fallbacks()}
        except Exception:
            offered = {}
        usable: list[tuple[ModelResolvedDelegationBackend, str, dict[str, str]]] = []
        for backend_id in (config.backend_id, *config.fallback_backend_ids):
            primary = backend_id == config.backend_id
            try:
                backend = resolve_backend() if primary else offered[backend_id]
                endpoint = _endpoint(
                    backend, backend_id, config.model if primary else None
                )
                headers = {
                    key: value
                    for key, value in backend.extra_headers.items()
                    if key.lower() not in _FORBIDDEN_HEADERS
                }
                headers["content-type"] = "application/json"
                headers["anthropic-version"] = version
                if beta is not None:
                    headers["anthropic-beta"] = beta
                if backend.secret_ref:
                    upstream_key = await read_secret(backend.secret_ref)
                    if not upstream_key:
                        raise ValueError("Upstream credential absent")
                    headers["x-api-key"] = upstream_key
            except Exception:
                continue
            usable.append((backend, endpoint, headers))
        return usable

    async def ready_route() -> None:
        if not await routes("2023-06-01", None):
            raise HTTPException(503, "Native gateway route is unavailable")

    @app.get("/onex/gateway/protocol")
    async def protocol(request: Request) -> dict[str, JsonValue]:
        await authenticate(request)
        await ready_route()
        return {
            "protocol": "onex-claude-gateway.v1",
            "streaming": True,
            "tool_use": True,
            "receipts": True,
            "backend_compatibility": "declared; inference proof required",
        }

    @app.get("/v1/models")
    async def models(request: Request) -> dict[str, JsonValue]:
        await authenticate(request)
        await ready_route()
        return {
            "data": [
                {"type": "model", "id": config.model, "display_name": config.model}
            ],
            "has_more": False,
        }

    async def proxy(request: Request, *, count_tokens: bool) -> Response:
        await authenticate(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > config.max_request_bytes:
                raise HTTPException(413, "Native request too large")
        try:
            body = _json_object(bytes(raw))
            if body.get("model") != config.model or not isinstance(
                body.get("messages"), list
            ):
                raise ValueError("Model or conversation missing")
            if "stream" in body and not isinstance(body["stream"], bool):
                raise ValueError("Invalid stream choice")
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Unsupported native model or request") from None
        ladder = await routes(
            request.headers.get("anthropic-version", "2023-06-01"),
            request.headers.get("anthropic-beta"),
        )
        if not ladder:
            raise HTTPException(503, "Native gateway route is unavailable")

        run_id = uuid4()
        started = time.monotonic()
        prompt_hash = hashlib.sha256(raw).hexdigest()
        backend = ladder[0][0]
        observation = _Observation(model=backend.model_id, limit=config.max_event_bytes)
        output = hashlib.sha256()
        failure: dict[str, int | str] = {}

        async def receipt(status: Literal["completed", "failed", "cancelled"]) -> None:
            await record(
                ModelClaudeGatewayReceipt(
                    run_id=run_id,
                    model=backend.model_id,
                    backend_id=backend.backend_id,
                    status=status,
                    in_tokens=_count(observation.usage.get("input_tokens")),
                    out_tokens=_count(observation.usage.get("output_tokens")),
                    cache_read_tokens=_count(
                        observation.usage.get("cache_read_input_tokens")
                    ),
                    cache_creation_tokens=_count(
                        observation.usage.get("cache_creation_input_tokens")
                    ),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    prompt_hash=prompt_hash,
                    output_hash=output.hexdigest(),
                    stop_reason=observation.stop_reason,
                    upstream_status=(
                        int(failure["status"]) if "status" in failure else None
                    ),
                    failure_reason=(
                        str(failure["reason"])[:500] if "reason" in failure else None
                    ),
                )
            )

        client: httpx.AsyncClient | None = None
        response: httpx.Response | None = None
        transferred = False
        try:
            for index, (backend, endpoint, headers) in enumerate(ladder):
                observation = _Observation(
                    model=backend.model_id, limit=config.max_event_bytes
                )
                # The primary's bytes go upstream untouched; a fallback serves the
                # same request under its own model id, every other field kept.
                content = (
                    bytes(raw)
                    if backend.model_id == config.model
                    else json.dumps({**body, "model": backend.model_id}).encode()
                )
                client = httpx.AsyncClient(
                    transport=transport,
                    timeout=backend.timeout_ms / 1000,
                    follow_redirects=False,
                    trust_env=False,
                )
                url = endpoint + "/count_tokens" if count_tokens else endpoint
                last = index == len(ladder) - 1
                try:
                    response = await client.send(
                        client.build_request(
                            "POST", url, headers=headers, content=content
                        ),
                        stream=True,
                    )
                except httpx.TransportError as error:
                    failure.clear()
                    failure["reason"] = type(error).__name__
                    if last:
                        raise
                    await client.aclose()
                    continue
                if response.status_code == 200:
                    break
                if last or not _fallback_eligible(response.status_code):
                    failure["status"] = response.status_code
                    failure_class = failure_class_for_status(response.status_code)
                    failure["reason"] = (
                        str(failure_class) if failure_class else "unclassified"
                    )
                    raise ValueError("Upstream request refused")
                await response.aclose()
                await client.aclose()
                response = None
            if response is None or client is None:
                raise ValueError("No native backend answered")
            if count_tokens:
                data = await _bounded_response(response, config.max_event_bytes)
                if _count(_json_object(data).get("input_tokens")) is None:
                    raise ValueError("Invalid token count")
                return Response(data, media_type="application/json")
            if body.get("stream") is True:
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise ValueError("Native response is not a stream")
                upstream = response

                async def stream() -> AsyncIterator[bytes]:
                    saved = False
                    try:
                        async with asyncio.timeout(backend.timeout_ms / 1000):
                            async for piece in upstream.aiter_bytes():
                                output.update(piece)
                                observation.feed(piece)
                                if observation.stopped and not saved:
                                    await receipt("completed")
                                    saved = True
                                yield piece
                        if not saved:
                            await receipt("failed")
                            saved = True
                            yield _SSE_ERROR
                    except asyncio.CancelledError:
                        if not saved:
                            with suppress(Exception):
                                await asyncio.shield(receipt("cancelled"))
                        raise
                    except Exception:
                        if not saved:
                            with suppress(Exception):
                                await receipt("failed")
                        yield _SSE_ERROR
                    finally:
                        await upstream.aclose()
                        await client.aclose()

                transferred = True
                return StreamingResponse(
                    stream(),
                    media_type="text/event-stream",
                    headers={"x-onex-run-id": str(run_id), "cache-control": "no-store"},
                )
            data = await _bounded_response(response, config.max_request_bytes)
            output.update(data)
            observation.read_body(_json_object(data))
            await receipt("completed")
            return Response(
                data,
                media_type="application/json",
                headers={"x-onex-run-id": str(run_id), "cache-control": "no-store"},
            )
        except asyncio.CancelledError:
            if not count_tokens:
                with suppress(Exception):
                    await asyncio.shield(receipt("cancelled"))
            raise
        except Exception:
            if not count_tokens:
                with suppress(Exception):
                    await receipt("failed")
            return JSONResponse(
                _ERROR, status_code=502, headers={"x-onex-run-id": str(run_id)}
            )
        finally:
            if not transferred:
                if response is not None:
                    await response.aclose()
                if client is not None:
                    await client.aclose()

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        return await proxy(request, count_tokens=False)

    @app.post("/v1/messages/count_tokens")
    async def count(request: Request) -> Response:
        return await proxy(request, count_tokens=True)

    return app


class HandlerClaudeGateway:
    """Serve native ingress with routing, credentials and evidence supplied by composition."""

    def __init__(
        self,
        *,
        resolve_backend: Callable[[], ModelResolvedDelegationBackend] | None = None,
        resolve_fallbacks: Callable[
            [], Sequence[ModelResolvedDelegationBackend]
        ] = tuple,
        read_secret: Callable[[str], Awaitable[str | None]] | None = None,
        record: Callable[[ModelClaudeGatewayReceipt], Awaitable[None]] | None = None,
    ) -> None:
        self._resolve = resolve_backend
        self._fallbacks = resolve_fallbacks
        self._secrets = read_secret
        self._record = record
        if any(
            value is not None for value in (resolve_backend, read_secret, record)
        ) and not all(
            value is not None for value in (resolve_backend, read_secret, record)
        ):
            raise ValueError("Native gateway dependencies must be supplied together")

    async def handle(self, request: ModelClaudeGatewayConfig) -> None:
        import uvicorn

        if self._resolve is None:
            from omnimarket.handlers.claude_gateway import (
                build_local_gateway_application,
            )

            application = build_local_gateway_application(request)
        else:
            assert self._secrets is not None
            assert self._record is not None
            application = build_gateway_app(
                request,
                resolve_backend=self._resolve,
                resolve_fallbacks=self._fallbacks,
                read_secret=self._secrets,
                record=self._record,
            )
        await uvicorn.Server(
            uvicorn.Config(
                application,
                host=request.bind_host,
                port=request.bind_port,
                access_log=False,
                log_level="warning",
            )
        ).serve()
