# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Codex runtime request adapter for Codex-facing OmniMarket skills."""

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
from uuid import UUID

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omnimarket.adapters.codex.topics import (
    TOPIC_CODEX_DELEGATE_SKILL_COMMAND,
    TOPIC_CODEX_PATTERN_B_DISPATCH_COMMAND,
    TOPIC_CODEX_PATTERN_B_DISPATCH_COMPLETED,
)
from omnimarket.adapters.wrapper_base import (
    collect_args,
    format_output,
    generate_correlation_id,
    handle_error,
    handle_timeout,
    map_args_to_payload,
)

_DEFAULT_COMMAND_TOPIC = TOPIC_CODEX_PATTERN_B_DISPATCH_COMMAND
_DEFAULT_RESPONSE_TOPIC = TOPIC_CODEX_PATTERN_B_DISPATCH_COMPLETED
_DEFAULT_REQUESTER = "codex"
_DELEGATE_SKILL_COMMAND_TOPIC = TOPIC_CODEX_DELEGATE_SKILL_COMMAND
_DELEGATE_SKILL_EVENT_TYPE = "omnimarket.delegate-skill"
_DELEGATE_SKILL_COMMAND_NAMES = frozenset(
    {"delegate_skill", "delegate_skill.orchestrate"}
)


class ModelDispatchBusRoute(BaseModel):
    """Wire shape for the Codex adapter dispatch route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_path: Path
    command_topic: str
    terminal_topic: str


class ModelDispatchBusCommand(BaseModel):
    """Envelope emitted by the Codex adapter to the runtime dispatch bus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_name: str
    requester: str
    payload: dict[str, object] = Field(default_factory=dict)
    correlation_id: UUID
    response_topic: str
    timeout_seconds: float
    target_runtime_address: str | None = None


class ModelDispatchBusTerminalResult(BaseModel):
    """Terminal result returned by the runtime dispatch bus."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    correlation_id: UUID | None = None
    status: str
    payload: dict[str, object] | None = None
    error_message: str | None = None


def default_command_topic() -> str:
    """Resolve the Codex adapter command topic."""
    return str(os.environ.get("ONEX_PATTERN_B_COMMAND_TOPIC", _DEFAULT_COMMAND_TOPIC))


def default_response_topic() -> str:
    """Resolve the Codex adapter response topic."""
    return str(os.environ.get("ONEX_PATTERN_B_RESPONSE_TOPIC", _DEFAULT_RESPONSE_TOPIC))


def default_requester() -> str:
    """Resolve the requester label attached to adapter commands."""
    return str(os.environ.get("ONEX_PATTERN_B_REQUESTER", _DEFAULT_REQUESTER))


def default_target_runtime_address() -> str | None:
    """Resolve the optional runtime address selector for adapter commands."""
    value = os.environ.get("ONEX_TARGET_RUNTIME_ADDRESS")
    if value is None or not value.strip():
        return None
    return value.strip()


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


class _CodexDispatchBusAdapter:
    """Minimal Codex runtime request adapter over an event bus transport."""

    def __init__(self, transport: _ProtocolLifecycleTransport, *, source: str) -> None:
        self._transport = transport
        self._source = source

    async def wait_for_result(
        self,
        route: ModelDispatchBusRoute,
        *,
        correlation_id: str,
        additional_terminal_topics: tuple[str, ...] = (),
    ) -> tuple[
        Callable[[], Awaitable[None]], asyncio.Queue[ModelDispatchBusTerminalResult]
    ]:
        q: asyncio.Queue[ModelDispatchBusTerminalResult] = asyncio.Queue()

        async def on_message(msg: object) -> None:
            if not isinstance(msg, ModelEventMessage):
                return
            result = _parse_terminal_result(msg.value)
            if result is None:
                return
            if str(result.correlation_id) == correlation_id:
                await q.put(result)

        topics: list[str] = [route.terminal_topic]
        for extra in additional_terminal_topics:
            if extra and extra not in topics:
                topics.append(extra)

        unsubs: list[object] = []
        for topic in topics:
            unsubs.append(
                await self._transport.subscribe(
                    topic,
                    None,
                    on_message,
                    group_id=f"codex-adapter-{correlation_id}",
                )
            )

        async def _unsubscribe() -> None:
            for unsub in unsubs:
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
        if _uses_direct_delegate_skill_contract(route, command):
            await self._publish_delegate_skill_command(route, command)
            return

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
            envelope.model_dump_json(exclude_none=True).encode("utf-8"),
            None,
        )

    async def _publish_delegate_skill_command(
        self,
        route: ModelDispatchBusRoute,
        command: ModelDispatchBusCommand,
    ) -> None:
        from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
            ModelDelegateSkillRequest,
        )

        payload = dict(command.payload)
        payload.setdefault("correlation_id", str(command.correlation_id))
        request = ModelDelegateSkillRequest.model_validate(payload)
        envelope = ModelEventEnvelope[ModelDelegateSkillRequest](
            payload=request,
            correlation_id=request.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=_DELEGATE_SKILL_EVENT_TYPE,
            source_tool=self._source,
        )
        await self._transport.publish(
            route.command_topic,
            None,
            envelope.model_dump_json(exclude_none=True).encode("utf-8"),
            None,
        )


CodexDispatchBusAdapter = _CodexDispatchBusAdapter


class ModelCodexRuntimeRequestAdapterError(BaseModel):
    """Structured error returned by the Codex runtime request adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    details: dict[str, object] | None = None
    retryable: bool | None = None


class ModelCodexRuntimeRequestAdapterRequest(BaseModel):
    """Request envelope for Codex skill dispatch through the runtime adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_name: str = Field(..., min_length=1)
    payload: dict[str, object] = Field(default_factory=dict)
    correlation_id: UUID | None = None
    timeout_ms: int = Field(default=300_000, gt=0, le=900_000)
    response_topic: str = Field(default_factory=default_response_topic, min_length=1)
    target_runtime_address: str | None = Field(
        default_factory=default_target_runtime_address
    )

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

    @field_validator("target_runtime_address")
    @classmethod
    def _validate_target_runtime_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if not normalized.startswith("runtime://"):
            raise ValueError("target_runtime_address must use runtime:// addressing")
        return normalized


class ModelCodexRuntimeRequestAdapterResponse(BaseModel):
    """Structured response returned by the Codex runtime request adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    command_name: str = Field(..., min_length=1)
    command_topic: str = Field(..., min_length=1)
    response_topic: str = Field(..., min_length=1)
    correlation_id: UUID | None = None
    dispatch_result: dict[str, object] | None = None
    output_payloads: list[dict[str, object]] | None = None
    error: ModelCodexRuntimeRequestAdapterError | None = None


def _build_client_request(
    *,
    command_name: str,
    payload: dict[str, object] | None = None,
    correlation_id: UUID | str | None = None,
    timeout_ms: int = 300_000,
    response_topic: str | None = None,
    target_runtime_address: str | None = None,
) -> ModelCodexRuntimeRequestAdapterRequest:
    return ModelCodexRuntimeRequestAdapterRequest.model_validate(
        {
            "command_name": command_name,
            "payload": payload or {},
            "correlation_id": correlation_id,
            "timeout_ms": timeout_ms,
            "response_topic": (
                response_topic
                if response_topic is not None
                else default_response_topic()
            ),
            "target_runtime_address": (
                target_runtime_address
                if target_runtime_address is not None
                else default_target_runtime_address()
            ),
        }
    )


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
    return map_args_to_payload(raw, omit_none=False)


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


def _uses_direct_delegate_skill_contract(
    route: ModelDispatchBusRoute,
    command: ModelDispatchBusCommand,
) -> bool:
    return (
        route.command_topic == _DELEGATE_SKILL_COMMAND_TOPIC
        and command.command_name in _DELEGATE_SKILL_COMMAND_NAMES
    )


def _parse_terminal_result(value: bytes) -> ModelDispatchBusTerminalResult | None:
    try:
        from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
            ModelDelegateSkillResponse,
        )

        delegate_envelope = ModelEventEnvelope[
            ModelDelegateSkillResponse
        ].model_validate_json(value)
        payload = delegate_envelope.payload
        dumped = cast(
            dict[str, object],
            payload.model_dump(mode="json", exclude_none=True),
        )
        error_message = payload.error_message or None
        return ModelDispatchBusTerminalResult(
            correlation_id=payload.correlation_id,
            status=payload.status,
            payload=dumped,
            error_message=error_message,
        )
    except Exception:
        pass

    try:
        envelope = ModelEventEnvelope[
            ModelDispatchBusTerminalResult
        ].model_validate_json(value)
        return envelope.payload
    except Exception:
        return None


def _build_dispatch_command(
    request: ModelCodexRuntimeRequestAdapterRequest,
    *,
    requester: str,
) -> ModelDispatchBusCommand:
    return ModelDispatchBusCommand(
        command_name=request.command_name,
        requester=requester,
        payload=request.payload,
        correlation_id=request.correlation_id or generate_correlation_id(),
        response_topic=request.response_topic,
        timeout_seconds=max(1.0, min(900.0, request.timeout_ms / 1000)),
        target_runtime_address=request.target_runtime_address,
    )


class CodexRuntimeRequestAdapter:
    """Codex-side adapter for runtime-dispatched OmniMarket skill requests."""

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

    def compile_request(
        self,
        *,
        command_name: str,
        payload: dict[str, object] | None = None,
        correlation_id: UUID | str | None = None,
        timeout_ms: int = 300_000,
        response_topic: str | None = None,
        target_runtime_address: str | None = None,
    ) -> ModelCodexRuntimeRequestAdapterResponse:
        """Compile the adapter command envelope without touching the event bus."""
        request = _build_client_request(
            command_name=command_name,
            payload=payload,
            correlation_id=correlation_id,
            timeout_ms=timeout_ms,
            response_topic=response_topic,
            target_runtime_address=target_runtime_address,
        )
        command = _build_dispatch_command(request, requester=self._requester)
        compiled_command = command.model_dump(mode="json", exclude_none=True)
        return ModelCodexRuntimeRequestAdapterResponse(
            ok=True,
            command_name=request.command_name,
            command_topic=self._command_topic,
            response_topic=request.response_topic,
            correlation_id=command.correlation_id,
            dispatch_result={
                "status": "compiled",
                "command_topic": self._command_topic,
                "response_topic": request.response_topic,
                "command": compiled_command,
            },
            output_payloads=[
                {
                    "status": "compiled",
                    "command_topic": self._command_topic,
                    "response_topic": request.response_topic,
                    "command": compiled_command,
                }
            ],
        )

    async def dispatch_async(
        self,
        *,
        command_name: str,
        payload: dict[str, object] | None = None,
        correlation_id: UUID | str | None = None,
        timeout_ms: int = 300_000,
        response_topic: str | None = None,
        additional_response_topics: tuple[str, ...] = (),
        target_runtime_address: str | None = None,
    ) -> ModelCodexRuntimeRequestAdapterResponse:
        request = _build_client_request(
            command_name=command_name,
            payload=payload,
            correlation_id=correlation_id,
            timeout_ms=timeout_ms,
            response_topic=response_topic,
            target_runtime_address=target_runtime_address,
        )

        transport = self._event_bus_factory()
        await transport.start()
        try:
            dispatch_adapter = CodexDispatchBusAdapter(
                transport, source=self._requester
            )
            route = ModelDispatchBusRoute(
                contract_path=Path("codex-runtime-request-adapter"),
                command_topic=self._command_topic,
                terminal_topic=request.response_topic,
            )
            command = _build_dispatch_command(request, requester=self._requester)
            unsubscribe, result_queue = await dispatch_adapter.wait_for_result(
                route,
                correlation_id=str(command.correlation_id),
                additional_terminal_topics=additional_response_topics,
            )
            try:
                await dispatch_adapter.publish_command(route, command)
                terminal_result = await asyncio.wait_for(
                    result_queue.get(),
                    timeout=command.timeout_seconds,
                )
            except TimeoutError:
                timeout_error = handle_timeout(
                    operation="Codex runtime adapter terminal result",
                    timeout_ms=request.timeout_ms,
                    correlation_id=command.correlation_id,
                )
                terminal_result = ModelDispatchBusTerminalResult(
                    correlation_id=command.correlation_id,
                    status="timeout",
                    error_message=timeout_error.message,
                )
            finally:
                await unsubscribe()
        finally:
            await transport.close()

        ok = terminal_result.status == "completed"
        if ok:
            return ModelCodexRuntimeRequestAdapterResponse(
                ok=True,
                command_name=request.command_name,
                command_topic=self._command_topic,
                response_topic=request.response_topic,
                correlation_id=terminal_result.correlation_id,
                dispatch_result=_dispatch_result(terminal_result),
                output_payloads=_output_payloads(terminal_result.payload),
            )
        return ModelCodexRuntimeRequestAdapterResponse(
            ok=False,
            command_name=request.command_name,
            command_topic=self._command_topic,
            response_topic=request.response_topic,
            correlation_id=terminal_result.correlation_id,
            dispatch_result=_dispatch_result(terminal_result),
            error=ModelCodexRuntimeRequestAdapterError(
                code=f"runtime_{terminal_result.status}",
                message=terminal_result.error_message
                or "Codex runtime adapter request did not complete successfully.",
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
        additional_response_topics: tuple[str, ...] = (),
        target_runtime_address: str | None = None,
    ) -> ModelCodexRuntimeRequestAdapterResponse:
        return asyncio.run(
            self.dispatch_async(
                command_name=command_name,
                payload=payload,
                correlation_id=correlation_id,
                timeout_ms=timeout_ms,
                response_topic=response_topic,
                additional_response_topics=additional_response_topics,
                target_runtime_address=target_runtime_address,
            )
        )


LocalRuntimeIngressClient = CodexRuntimeRequestAdapter


def _response_to_exit_code(response: ModelCodexRuntimeRequestAdapterResponse) -> int:
    return 0 if response.ok else 1


def _build_cli_error_response(
    *,
    command_name: str,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> ModelCodexRuntimeRequestAdapterResponse:
    return ModelCodexRuntimeRequestAdapterResponse(
        ok=False,
        command_name=command_name,
        command_topic=default_command_topic(),
        response_topic=default_response_topic(),
        error=ModelCodexRuntimeRequestAdapterError(
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
        help="Logical OmniMarket command name",
    )
    parser.add_argument(
        "--payload",
        help="Inline JSON object payload forwarded through the Codex runtime adapter",
    )
    parser.add_argument(
        "--payload-file",
        help="Path to a JSON file containing the payload object",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=300_000,
        help="Dispatch timeout passed to the Codex runtime adapter",
    )
    parser.add_argument(
        "--response-topic",
        default=default_response_topic(),
        help="Adapter response topic used for correlated terminal results",
    )
    parser.add_argument(
        "--correlation-id",
        help="Optional correlation UUID",
    )
    parser.add_argument(
        "--target-runtime-address",
        default=default_target_runtime_address(),
        help="Optional runtime:// address to target for this adapter request",
    )
    parser.add_argument(
        "--compile-only",
        action="store_true",
        help=(
            "Validate and print the adapter command envelope without publishing "
            "to the event bus"
        ),
    )
    args = parser.parse_args(argv)
    parsed_args = collect_args(args, include_none=True)

    try:
        payload = _load_payload(
            payload=cast(str | None, parsed_args["payload"]),
            payload_file=cast(str | None, parsed_args["payload_file"]),
        )
        correlation_id = cast(str | None, parsed_args["correlation_id"])
        if correlation_id is None:
            embedded_correlation_id = payload.get("correlation_id")
            if (
                isinstance(embedded_correlation_id, str)
                and embedded_correlation_id.strip()
            ):
                correlation_id = embedded_correlation_id
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
        client = CodexRuntimeRequestAdapter()
        if args.compile_only:
            response = client.compile_request(
                command_name=args.command_name,
                payload=payload,
                correlation_id=correlation_id,
                timeout_ms=args.timeout_ms,
                response_topic=args.response_topic,
                target_runtime_address=args.target_runtime_address,
            )
        else:
            response = client.dispatch_sync(
                command_name=args.command_name,
                payload=payload,
                correlation_id=correlation_id,
                timeout_ms=args.timeout_ms,
                response_topic=args.response_topic,
                target_runtime_address=args.target_runtime_address,
            )
    except ValidationError as exc:
        response = _build_cli_error_response(
            command_name=args.command_name,
            code="payload_invalid",
            message="Invalid Codex runtime request adapter request",
            details={"errors": json.loads(exc.json(include_url=False))},
        )
    except (OSError, ValueError, RuntimeError) as exc:
        error = handle_error(exc, code="runtime_adapter_error")
        response = _build_cli_error_response(
            command_name=args.command_name,
            code=error.code,
            message=error.message,
        )

    sys.stdout.write(format_output(response) + "\n")
    return _response_to_exit_code(response)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CodexRuntimeRequestAdapter",
    "LocalRuntimeIngressClient",
    "ModelCodexRuntimeRequestAdapterError",
    "ModelCodexRuntimeRequestAdapterRequest",
    "ModelCodexRuntimeRequestAdapterResponse",
    "default_command_topic",
    "default_requester",
    "default_response_topic",
    "default_target_runtime_address",
    "main",
]
