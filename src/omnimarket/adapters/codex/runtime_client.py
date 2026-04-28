# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pattern B broker client for Codex-facing OmniMarket skills."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omnimarket.adapters.codex.topics import (
    TOPIC_CODEX_PATTERN_B_DISPATCH_COMMAND,
    TOPIC_CODEX_PATTERN_B_DISPATCH_COMPLETED,
)

_DEFAULT_COMMAND_TOPIC = TOPIC_CODEX_PATTERN_B_DISPATCH_COMMAND
_DEFAULT_RESPONSE_TOPIC = TOPIC_CODEX_PATTERN_B_DISPATCH_COMPLETED
_DEFAULT_REQUESTER = "codex"


class ModelDispatchBusRoute(BaseModel):
    """Wire shape for a Pattern B broker routing key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_path: Path
    command_topic: str
    terminal_topic: str


class ModelDispatchBusCommand(BaseModel):
    """Envelope for a Pattern B broker dispatch command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_name: str
    requester: str
    payload: dict[str, object] = Field(default_factory=dict)
    correlation_id: UUID
    response_topic: str
    timeout_seconds: float


class ModelDispatchBusTerminalResult(BaseModel):
    """Terminal result returned by the Pattern B broker."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    correlation_id: UUID | None = None
    status: str
    payload: dict[str, object] | None = None
    error_message: str | None = None


def default_command_topic() -> str:
    """Resolve the Pattern B broker command topic."""
    return str(os.environ.get("ONEX_PATTERN_B_COMMAND_TOPIC", _DEFAULT_COMMAND_TOPIC))


def default_response_topic() -> str:
    """Resolve the Pattern B broker response topic."""
    return str(os.environ.get("ONEX_PATTERN_B_RESPONSE_TOPIC", _DEFAULT_RESPONSE_TOPIC))


def default_requester() -> str:
    """Resolve the requester label attached to broker commands."""
    return str(os.environ.get("ONEX_PATTERN_B_REQUESTER", _DEFAULT_REQUESTER))


class _ProtocolLifecycleTransport(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: object = None
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        node_identity: object,
        on_message: object,
        **kwargs: object,
    ) -> object: ...


class _DispatchBusClient:
    """Minimal Pattern B broker client over an event bus transport."""

    def __init__(self, transport: _ProtocolLifecycleTransport, *, source: str) -> None:
        self._transport = transport
        self._source = source

    async def wait_for_result(
        self,
        route: ModelDispatchBusRoute,
        *,
        correlation_id: str,
    ) -> tuple[
        Callable[[], Awaitable[None]], asyncio.Queue[ModelDispatchBusTerminalResult]
    ]:
        q: asyncio.Queue[ModelDispatchBusTerminalResult] = asyncio.Queue()

        async def on_message(msg: object) -> None:
            if not isinstance(msg, ModelEventMessage):
                return
            try:
                envelope = ModelEventEnvelope[
                    ModelDispatchBusTerminalResult
                ].model_validate_json(msg.value)
            except Exception:
                return
            if str(envelope.payload.correlation_id) == correlation_id:
                await q.put(envelope.payload)

        unsub = await self._transport.subscribe(
            route.terminal_topic,
            None,
            on_message,
            group_id=f"codex-broker-{correlation_id}",
        )

        async def _unsubscribe() -> None:
            if callable(unsub):
                try:
                    result = unsub()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass

        return _unsubscribe, q

    async def publish_command(
        self,
        route: ModelDispatchBusRoute,
        command: ModelDispatchBusCommand,
    ) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand](
            payload=command,
            correlation_id=command.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=route.command_topic,
            source_tool=self._source,
        )
        await self._transport.publish(
            route.command_topic,
            None,
            envelope.model_dump_json().encode("utf-8"),
            None,
        )


DispatchBusClient = _DispatchBusClient


class ModelPatternBBrokerClientError(BaseModel):
    """Structured error returned by the broker client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    details: dict[str, object] | None = None
    retryable: bool | None = None


class ModelPatternBBrokerClientRequest(BaseModel):
    """Request envelope for Codex skill dispatch over Pattern B."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_name: str = Field(..., min_length=1)
    payload: dict[str, object] = Field(default_factory=dict)
    correlation_id: UUID | None = None
    timeout_ms: int = Field(default=300_000, gt=0, le=900_000)
    response_topic: str = Field(default_factory=default_response_topic, min_length=1)

    @field_validator("command_name")
    @classmethod
    def _validate_command_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("command_name must be a non-empty string")
        return normalized

    @field_validator("response_topic")
    @classmethod
    def _validate_response_topic(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("response_topic must be a non-empty string")
        return normalized


class ModelPatternBBrokerClientResponse(BaseModel):
    """Structured response returned by the Pattern B broker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    command_name: str = Field(..., min_length=1)
    command_topic: str = Field(..., min_length=1)
    response_topic: str = Field(..., min_length=1)
    correlation_id: UUID | None = None
    dispatch_result: dict[str, object] | None = None
    output_payloads: list[dict[str, object]] | None = None
    error: ModelPatternBBrokerClientError | None = None


def _default_event_bus_factory() -> _ProtocolLifecycleTransport:
    return cast(_ProtocolLifecycleTransport, EventBusKafka.default())


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


def _output_payloads(payload: object) -> list[dict[str, object]] | None:
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return cast(list[dict[str, object]], payload)
    return None


def _dispatch_result(
    result: ModelDispatchBusTerminalResult,
) -> dict[str, object]:
    return cast(dict[str, object], result.model_dump(mode="json", exclude_none=True))


class PatternBBrokerClient:
    """Async client for broker-backed skill dispatch."""

    def __init__(
        self,
        *,
        event_bus_factory: Callable[[], _ProtocolLifecycleTransport] | None = None,
        requester: str | None = None,
        command_topic: str | None = None,
    ) -> None:
        self._event_bus_factory = event_bus_factory or _default_event_bus_factory
        self._requester = requester or default_requester()
        self._command_topic = command_topic or default_command_topic()

    async def dispatch_async(
        self,
        *,
        command_name: str,
        payload: dict[str, object] | None = None,
        correlation_id: UUID | str | None = None,
        timeout_ms: int = 300_000,
        response_topic: str | None = None,
    ) -> ModelPatternBBrokerClientResponse:
        request = ModelPatternBBrokerClientRequest.model_validate(
            {
                "command_name": command_name,
                "payload": payload or {},
                "correlation_id": correlation_id,
                "timeout_ms": timeout_ms,
                "response_topic": response_topic or default_response_topic(),
            }
        )

        transport = self._event_bus_factory()
        await transport.start()
        try:
            broker_client = DispatchBusClient(transport, source=self._requester)
            route = ModelDispatchBusRoute(
                contract_path=Path("pattern-b-broker"),
                command_topic=self._command_topic,
                terminal_topic=request.response_topic,
            )
            command = ModelDispatchBusCommand(
                command_name=request.command_name,
                requester=self._requester,
                payload=request.payload,
                correlation_id=request.correlation_id or uuid4(),
                response_topic=request.response_topic,
                timeout_seconds=max(1.0, min(900.0, request.timeout_ms / 1000)),
            )
            unsubscribe, result_queue = await broker_client.wait_for_result(
                route,
                correlation_id=str(command.correlation_id),
            )
            try:
                await broker_client.publish_command(route, command)
                terminal_result = await asyncio.wait_for(
                    result_queue.get(),
                    timeout=command.timeout_seconds,
                )
            except TimeoutError:
                terminal_result = ModelDispatchBusTerminalResult(
                    correlation_id=command.correlation_id,
                    status="timeout",
                    error_message="Timed out waiting for Pattern B broker terminal result.",
                )
            finally:
                await unsubscribe()
        finally:
            await transport.close()

        ok = terminal_result.status == "completed"
        if ok:
            return ModelPatternBBrokerClientResponse(
                ok=True,
                command_name=request.command_name,
                command_topic=self._command_topic,
                response_topic=request.response_topic,
                correlation_id=terminal_result.correlation_id,
                dispatch_result=_dispatch_result(terminal_result),
                output_payloads=_output_payloads(terminal_result.payload),
            )
        return ModelPatternBBrokerClientResponse(
            ok=False,
            command_name=request.command_name,
            command_topic=self._command_topic,
            response_topic=request.response_topic,
            correlation_id=terminal_result.correlation_id,
            dispatch_result=_dispatch_result(terminal_result),
            error=ModelPatternBBrokerClientError(
                code=f"broker_{terminal_result.status}",
                message=terminal_result.error_message
                or "Pattern B broker request did not complete successfully.",
                retryable=terminal_result.status == "timeout",
            ),
        )

    def dispatch_sync(
        self,
        *,
        command_name: str,
        payload: dict[str, object] | None = None,
        correlation_id: UUID | str | None = None,
        timeout_ms: int = 300_000,
        response_topic: str | None = None,
    ) -> ModelPatternBBrokerClientResponse:
        return asyncio.run(
            self.dispatch_async(
                command_name=command_name,
                payload=payload,
                correlation_id=correlation_id,
                timeout_ms=timeout_ms,
                response_topic=response_topic,
            )
        )


LocalRuntimeIngressClient = PatternBBrokerClient


def _response_to_exit_code(response: ModelPatternBBrokerClientResponse) -> int:
    return 0 if response.ok else 1


def _build_cli_error_response(
    *,
    command_name: str,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> ModelPatternBBrokerClientResponse:
    return ModelPatternBBrokerClientResponse(
        ok=False,
        command_name=command_name,
        command_topic=default_command_topic(),
        response_topic=default_response_topic(),
        error=ModelPatternBBrokerClientError(
            code=code,
            message=message,
            details=details,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--command-name",
        "--node-alias",
        dest="command_name",
        required=True,
        help="Logical Pattern B command name",
    )
    parser.add_argument(
        "--payload",
        help="Inline JSON object payload forwarded to the Pattern B broker",
    )
    parser.add_argument(
        "--payload-file",
        help="Path to a JSON file containing the payload object",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=300_000,
        help="Dispatch timeout passed to the Pattern B broker",
    )
    parser.add_argument(
        "--response-topic",
        default=default_response_topic(),
        help="Broker response topic used for correlated terminal results",
    )
    parser.add_argument(
        "--correlation-id",
        help="Optional correlation UUID",
    )
    args = parser.parse_args(argv)

    try:
        payload = _load_payload(payload=args.payload, payload_file=args.payload_file)
    except FileNotFoundError as exc:
        response = _build_cli_error_response(
            command_name=args.command_name,
            code="payload_file_missing",
            message=str(exc),
        )
        sys.stdout.write(response.model_dump_json(indent=2) + "\n")
        return _response_to_exit_code(response)
    except json.JSONDecodeError as exc:
        response = _build_cli_error_response(
            command_name=args.command_name,
            code="payload_invalid",
            message=f"Invalid JSON payload: {exc}",
        )
        sys.stdout.write(response.model_dump_json(indent=2) + "\n")
        return _response_to_exit_code(response)

    try:
        client = PatternBBrokerClient()
        response = client.dispatch_sync(
            command_name=args.command_name,
            payload=payload,
            correlation_id=args.correlation_id,
            timeout_ms=args.timeout_ms,
            response_topic=args.response_topic,
        )
    except ValidationError as exc:
        response = _build_cli_error_response(
            command_name=args.command_name,
            code="payload_invalid",
            message="Invalid Pattern B broker client request",
            details={"errors": json.loads(exc.json(include_url=False))},
        )
    except (OSError, ValueError, RuntimeError) as exc:
        response = _build_cli_error_response(
            command_name=args.command_name,
            code="broker_client_error",
            message=str(exc),
        )

    sys.stdout.write(response.model_dump_json(indent=2) + "\n")
    return _response_to_exit_code(response)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LocalRuntimeIngressClient",
    "ModelPatternBBrokerClientError",
    "ModelPatternBBrokerClientRequest",
    "ModelPatternBBrokerClientResponse",
    "PatternBBrokerClient",
    "default_command_topic",
    "default_requester",
    "default_response_topic",
    "main",
]
