# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise bounded POST behavior against a real local HTTP socket."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest

from omnimarket.handlers.rightmodeler_validation.http_transport import (
    RightModelerHttpTransport,
    RightModelerServiceTransportError,
)
from omnimarket.models.model_swap_validation_service.model_model_swap_service_config import (
    ModelModelSwapServiceConfig,
)

SECRET = "service-credential-ascii"


class SecretStore:
    def __init__(self, value: str | None = SECRET) -> None:
        self.value = value
        self.refs: list[str] = []

    async def get_secret(self, key: str) -> str | None:
        self.refs.append(key)
        return self.value


class Request(NamedTuple):
    headers: bytes
    body: bytes


@asynccontextmanager
async def local_service(
    body: bytes,
    *,
    status: int = 200,
    hold: asyncio.Event | None = None,
    response_headers: dict[str, str] | None = None,
) -> AsyncIterator[tuple[str, list[Request], asyncio.Event]]:
    requests: list[Request] = []
    received = asyncio.Event()
    workers: set[asyncio.Task[None]] = set()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            content_length = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            payload = await reader.readexactly(content_length)
            requests.append(Request(headers, payload))
            received.set()
            if hold is not None:
                await hold.wait()
            writer.write(
                f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n".encode()
                + b"".join(
                    f"{name}: {value}\r\n".encode()
                    for name, value in (response_headers or {}).items()
                )
                + b"Location: /must-not-follow\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(handle(reader, writer))
        workers.add(task)
        task.add_done_callback(workers.discard)

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/exact/validate", requests, received
    finally:
        server.close()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await server.wait_closed()


def config(endpoint: str, **changes: object) -> ModelModelSwapServiceConfig:
    return ModelModelSwapServiceConfig.model_validate(
        {
            "endpoint_ref": endpoint,
            "secret_ref": "rightmodeler.service.api_key",
            "deadline_seconds": 5.0,
            "connect_timeout_seconds": 1.0,
            "read_timeout_seconds": 1.0,
            "write_timeout_seconds": 1.0,
            "pool_timeout_seconds": 1.0,
            "max_request_bytes": 4096,
            "max_response_bytes": 1024,
            "response_chunk_bytes": 16,
            **changes,
        }
    )


async def test_one_exact_post_uses_explicit_store_and_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    store = SecretStore("service-credential-ascii")
    async with local_service(b'{"evidence_only":true}') as (endpoint, requests, _):
        response = await RightModelerHttpTransport(
            config(endpoint), secret_store=store
        ).post(b'{"correlation_id":"example"}')
    assert response == b'{"evidence_only":true}'
    assert len(requests) == 1
    assert requests[0].headers.startswith(b"POST /exact/validate HTTP/1.1\r\n")
    assert b"authorization: Bearer service-credential-ascii\r\n" in requests[0].headers
    assert requests[0].body == b'{"correlation_id":"example"}'
    assert store.refs == ["rightmodeler.service.api_key"]


@pytest.mark.parametrize("status", [202, 302, 401, 429, 500])
async def test_nonterminal_http_status_never_retries_or_follows(status: int) -> None:
    async with local_service(b"sensitive error body", status=status) as (
        endpoint,
        requests,
        _,
    ):
        with pytest.raises(RightModelerServiceTransportError) as error:
            await RightModelerHttpTransport(
                config(endpoint), secret_store=SecretStore("ascii-secret")
            ).post(b"{}")
    assert error.value.failure.value == "tool_failed"
    assert str(error.value) == "service_http_error"
    assert len(requests) == 1


@pytest.mark.parametrize("size", [31, 32, 33])
@pytest.mark.parametrize("encoding", [None, "identity", "IDENTITY"])
async def test_streaming_response_byte_boundary(
    size: int, encoding: str | None
) -> None:
    body = json.dumps("x" * (size - 2)).encode()
    headers = {"Content-Encoding": encoding} if encoding is not None else {}
    async with local_service(body, response_headers=headers) as (endpoint, _, _):
        transport = RightModelerHttpTransport(
            config(endpoint, max_response_bytes=32),
            secret_store=SecretStore("ascii-secret"),
        )
        if size <= 32:
            assert await transport.post(b"{}") == body
        else:
            with pytest.raises(RightModelerServiceTransportError) as error:
                await transport.post(b"{}")
            assert error.value.code == "service_response_too_large"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_compressed_response_is_refused_before_body_read_or_decompression(
    encoding: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_attempts: list[str] = []

    async def unexpected_stream(
        self: httpx.Response, chunk_size: int | None = None
    ) -> AsyncIterator[bytes]:
        read_attempts.append("read")
        yield b"{}"

    monkeypatch.setattr(httpx.Response, "aiter_bytes", unexpected_stream)
    monkeypatch.setattr(httpx.Response, "aiter_raw", unexpected_stream)
    async with local_service(
        b"must never be decompressed", response_headers={"Content-Encoding": encoding}
    ) as (endpoint, requests, _):
        with pytest.raises(RightModelerServiceTransportError) as error:
            await RightModelerHttpTransport(
                config(endpoint), secret_store=SecretStore()
            ).post(b"{}")
    assert error.value.failure.value == "schema_mismatch"
    assert error.value.code == "service_unsupported_encoding"
    assert len(requests) == 1
    assert read_attempts == []


@pytest.mark.parametrize("size", [1, 2, 3])
async def test_request_byte_boundary(size: int) -> None:
    async with local_service(b"{}") as (endpoint, requests, _):
        transport = RightModelerHttpTransport(
            config(endpoint, max_request_bytes=2), secret_store=SecretStore()
        )
        if size <= 2:
            assert await transport.post(b"x" * size) == b"{}"
            assert len(requests) == 1
        else:
            with pytest.raises(RightModelerServiceTransportError) as error:
                await transport.post(b"x" * size)
            assert error.value.code == "service_request_too_large"
            assert requests == []


@pytest.mark.parametrize("store", [None, SecretStore(None)])
async def test_credentials_require_explicit_store_without_environment_fallback(
    monkeypatch: pytest.MonkeyPatch, store: SecretStore | None
) -> None:
    monkeypatch.setenv("RIGHTMODELER_SERVICE_API_KEY", "must-not-be-used")
    async with local_service(b"{}") as (endpoint, requests, _):
        with pytest.raises(RightModelerServiceTransportError) as error:
            await RightModelerHttpTransport(config(endpoint), secret_store=store).post(
                b"{}"
            )
    assert error.value.failure.value == "needs_input"
    assert requests == []


async def test_explicit_resolver_config_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = tmp_path / "resolver.yaml"
    resolver.write_text(
        "mappings:\n"
        "  - logical_name: rightmodeler.service.api_key\n"
        "    source:\n"
        "      source_type: env\n"
        "      source_path: EXPLICIT_SERVICE_CREDENTIAL\n"
    )
    monkeypatch.setenv("EXPLICIT_SERVICE_CREDENTIAL", "explicit-mapped-secret")
    monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", "/not/the/selected/config")
    async with local_service(b"{}") as (endpoint, requests, _):
        await RightModelerHttpTransport(
            config(endpoint, secret_resolver_config_path=str(resolver))
        ).post(b"{}")
    assert b"authorization: Bearer explicit-mapped-secret\r\n" in requests[0].headers


@pytest.mark.parametrize("escaped", [False, True])
async def test_response_credential_echo_is_refused(escaped: bool) -> None:
    secret = 'ascii-credential-"quote'
    body = json.dumps({"tool_error_code": secret}).encode()
    if escaped:
        body = body.replace(b"a", b"\\u0061")
    async with local_service(body) as (endpoint, _, _):
        with pytest.raises(RightModelerServiceTransportError) as error:
            await RightModelerHttpTransport(
                config(endpoint), secret_store=SecretStore(secret)
            ).post(b"{}")
    assert error.value.code == "service_credential_echo"
    assert secret not in str(error.value)


async def test_deadline_after_server_receives_request_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_timeout = asyncio.timeout
    deadlines: list[asyncio.Timeout] = []

    def observed_timeout(delay: float | None) -> asyncio.Timeout:
        deadline = original_timeout(delay)
        deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(asyncio, "timeout", observed_timeout)
    hold = asyncio.Event()
    async with local_service(b"{}", hold=hold) as (endpoint, requests, received):
        transport = RightModelerHttpTransport(
            config(endpoint), secret_store=SecretStore("ascii-secret")
        )
        pending = asyncio.create_task(transport.post(b"{}"))
        await asyncio.wait_for(received.wait(), timeout=5)
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(RightModelerServiceTransportError) as error:
            await asyncio.wait_for(pending, timeout=5)
        assert error.value.failure.value == "timeout"
        assert len(requests) == 1


async def test_async_cancellation_propagates_without_retry() -> None:
    async with local_service(b"{}", hold=asyncio.Event()) as (
        endpoint,
        requests,
        received,
    ):
        pending = asyncio.create_task(
            RightModelerHttpTransport(
                config(endpoint), secret_store=SecretStore("ascii-secret")
            ).post(b"{}")
        )
        await asyncio.wait_for(received.wait(), timeout=5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=5)
        assert len(requests) == 1
