# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Local runtime ingress client for Codex-facing OmniMarket skills."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_DEFAULT_SOCKET_PATH = "/tmp/onex-runtime.sock"
_RECV_BUFSIZE = 4096
_MAX_RESPONSE_SIZE = 1_048_576
_MAX_READ_ITERATIONS = 64


def default_socket_path() -> str:
    """Resolve the runtime ingress socket path."""
    return os.environ.get("ONEX_RUNTIME_SOCKET_PATH", _DEFAULT_SOCKET_PATH)


class ModelLocalRuntimeClientError(BaseModel):
    """Structured error returned by the runtime ingress client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    details: dict[str, object] | None = None
    retryable: bool | None = None


class ModelLocalRuntimeClientRequest(BaseModel):
    """Request envelope for the runtime ingress client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_alias: str = Field(..., min_length=1)
    payload: dict[str, object] = Field(default_factory=dict)
    correlation_id: UUID | None = None
    timeout_ms: int = Field(default=300_000, gt=0, le=900_000)

    @field_validator("node_alias")
    @classmethod
    def _validate_node_alias(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("node_alias must be a non-empty string")
        return normalized


class ModelLocalRuntimeClientResponse(BaseModel):
    """Structured response returned by the runtime ingress."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    node_alias: str = Field(..., min_length=1)
    resolved_node_name: str | None = None
    contract_name: str | None = None
    topic: str | None = None
    terminal_event: str | None = None
    correlation_id: UUID | None = None
    dispatch_result: dict[str, object] | None = None
    output_payloads: list[dict[str, object]] | None = None
    error: ModelLocalRuntimeClientError | None = None


class LocalRuntimeIngressClient:
    """Synchronous Unix-socket client for the runtime local ingress."""

    def __init__(
        self,
        socket_path: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._socket_path = socket_path or default_socket_path()
        self._timeout_seconds = timeout_seconds
        self._sock: socket.socket | None = None
        self._buf = bytearray()

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._timeout_seconds)
            sock.connect(self._socket_path)
        except Exception:
            sock.close()
            raise
        self._sock = sock
        self._buf = bytearray()
        return sock

    def _read_response(self, sock: socket.socket) -> dict[str, object]:
        iterations = 0
        while b"\n" not in self._buf:
            chunk = sock.recv(_RECV_BUFSIZE)
            if not chunk:
                raise ConnectionResetError("runtime ingress closed connection")
            self._buf.extend(chunk)
            iterations += 1
            if len(self._buf) > _MAX_RESPONSE_SIZE:
                raise ValueError("runtime ingress response exceeded size limit")
            if iterations >= _MAX_READ_ITERATIONS:
                raise ValueError("runtime ingress response exceeded read limit")
        idx = self._buf.index(b"\n")
        line = self._buf[:idx]
        self._buf = self._buf[idx + 1 :]
        raw = json.loads(line.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("runtime ingress returned a non-object JSON response")
        return raw

    def _send_and_recv(
        self, request: ModelLocalRuntimeClientRequest
    ) -> dict[str, object]:
        line = request.model_dump_json().encode("utf-8") + b"\n"
        try:
            sock = self._connect()
            sock.sendall(line)
            return self._read_response(sock)
        except OSError:
            self.close()
            sock = self._connect()
            sock.sendall(line)
            return self._read_response(sock)

    def dispatch_sync(
        self,
        *,
        node_alias: str,
        payload: dict[str, object] | None = None,
        correlation_id: UUID | str | None = None,
        timeout_ms: int = 300_000,
    ) -> ModelLocalRuntimeClientResponse:
        """Dispatch a node request through the runtime local ingress."""

        request = ModelLocalRuntimeClientRequest.model_validate(
            {
                "node_alias": node_alias,
                "payload": payload or {},
                "correlation_id": correlation_id,
                "timeout_ms": timeout_ms,
            }
        )
        raw = self._send_and_recv(request)
        return ModelLocalRuntimeClientResponse.model_validate(raw)

    def close(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
            self._buf = bytearray()

    def __enter__(self) -> LocalRuntimeIngressClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()


def _load_payload(
    *, payload: str | None, payload_file: str | None
) -> dict[str, object]:
    if payload is not None and payload_file is not None:
        raise ValueError("Specify at most one of --payload or --payload-file")

    if payload_file is not None:
        raw = json.loads(Path(payload_file).read_text(encoding="utf-8"))
    elif payload is not None:
        raw = json.loads(payload)
    else:
        raw = {}

    if not isinstance(raw, dict):
        raise ValueError("Payload must decode to a JSON object")
    return raw


def _response_to_exit_code(response: ModelLocalRuntimeClientResponse) -> int:
    return 0 if response.ok else 1


def _build_cli_error_response(
    *,
    node_alias: str,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> ModelLocalRuntimeClientResponse:
    return ModelLocalRuntimeClientResponse(
        ok=False,
        node_alias=node_alias,
        error=ModelLocalRuntimeClientError(
            code=code,
            message=message,
            details=details,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-alias", required=True, help="Contract or node alias")
    parser.add_argument(
        "--payload",
        help="Inline JSON object payload forwarded to the runtime ingress",
    )
    parser.add_argument(
        "--payload-file",
        help="Path to a JSON file containing the payload object",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=300_000,
        help="Dispatch timeout passed to the runtime ingress",
    )
    parser.add_argument(
        "--socket-path",
        default=default_socket_path(),
        help="Unix socket path for the runtime ingress",
    )
    parser.add_argument(
        "--socket-timeout-seconds",
        type=float,
        default=5.0,
        help="Socket connect/read timeout in seconds",
    )
    parser.add_argument(
        "--correlation-id",
        help="Optional correlation UUID",
    )
    args = parser.parse_args(argv)

    try:
        payload = _load_payload(payload=args.payload, payload_file=args.payload_file)
        with LocalRuntimeIngressClient(
            socket_path=args.socket_path,
            timeout_seconds=args.socket_timeout_seconds,
        ) as client:
            response = client.dispatch_sync(
                node_alias=args.node_alias,
                payload=payload,
                correlation_id=args.correlation_id,
                timeout_ms=args.timeout_ms,
            )
    except FileNotFoundError as exc:
        response = _build_cli_error_response(
            node_alias=args.node_alias,
            code="payload_file_missing",
            message=str(exc),
        )
    except json.JSONDecodeError as exc:
        response = _build_cli_error_response(
            node_alias=args.node_alias,
            code="payload_invalid",
            message=f"Invalid JSON payload: {exc}",
        )
    except ValidationError as exc:
        response = _build_cli_error_response(
            node_alias=args.node_alias,
            code="payload_invalid",
            message="Invalid local runtime client request",
            details={"errors": json.loads(exc.json(include_url=False))},
        )
    except (OSError, ValueError) as exc:
        response = _build_cli_error_response(
            node_alias=args.node_alias,
            code="runtime_client_error",
            message=str(exc),
        )

    sys.stdout.write(response.model_dump_json(indent=2) + "\n")
    return _response_to_exit_code(response)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LocalRuntimeIngressClient",
    "ModelLocalRuntimeClientError",
    "ModelLocalRuntimeClientRequest",
    "ModelLocalRuntimeClientResponse",
    "default_socket_path",
    "main",
]
