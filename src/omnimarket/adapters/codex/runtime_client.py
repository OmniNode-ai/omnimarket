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
from typing import Any, Protocol, cast
from uuid import UUID

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omnimarket.adapters.codex.topics import (
    TOPIC_CODEX_DELEGATE_SKILL_COMMAND,
    TOPIC_CODEX_DELEGATE_SKILL_COMPLETED,
    TOPIC_CODEX_DELEGATE_SKILL_FAILED,
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
# The runtime publishes delegate-skill results to the contract-declared terminal
# topics, not to the generic pattern-b-dispatch-completed topic.  Subscribing to
# the wrong topic is the Pattern B ephemeral consumer race (OMN-11991).
_DELEGATE_SKILL_COMPLETED_TOPIC = TOPIC_CODEX_DELEGATE_SKILL_COMPLETED
_DELEGATE_SKILL_FAILED_TOPIC = TOPIC_CODEX_DELEGATE_SKILL_FAILED
_RUNTIME_SELECTION_VALUES = frozenset({"deployed", "local", "deployed_or_local"})
_TERMINAL_SUBSCRIPTION_READY_TIMEOUT_SECONDS = 15.0
_TERMINAL_SUBSCRIPTION_READY_POLL_SECONDS = 0.05
_NODE_ROOT = Path(__file__).resolve().parents[2] / "nodes"


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


class ModelRuntimeEvidence(BaseModel):
    """Runtime/backend evidence returned by the adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_runtime_mode: str
    event_bus_backend: str
    state_store_backend: str
    node_contract: str | None = None
    command_topic: str
    terminal_topic: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


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


def default_runtime_selection() -> str:
    """Resolve the explicit runtime selection mode for adapter dispatch."""
    value = os.environ.get("ONEX_RUNTIME_SELECTION", "deployed").strip()
    return value or "deployed"


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
        readiness_timeout_seconds: float = (
            _TERMINAL_SUBSCRIPTION_READY_TIMEOUT_SECONDS
        ),
    ) -> tuple[
        Callable[[], Awaitable[None]], asyncio.Queue[ModelDispatchBusTerminalResult]
    ]:
        q: asyncio.Queue[ModelDispatchBusTerminalResult] = asyncio.Queue()

        async def on_message(msg: object) -> None:
            value = getattr(msg, "value", None)
            if isinstance(value, bytearray):
                value = bytes(value)
            if not isinstance(value, bytes):
                return
            result = _parse_terminal_result(value)
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
                    required_for_readiness=True,
                )
            )

        await _await_terminal_subscriptions_ready(
            self._transport,
            timeout_seconds=readiness_timeout_seconds,
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
        payload["correlation_id"] = str(command.correlation_id)
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
    runtime_selection: str = Field(default_factory=default_runtime_selection)

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

    @field_validator("runtime_selection")
    @classmethod
    def _validate_runtime_selection(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in _RUNTIME_SELECTION_VALUES:
            allowed = ", ".join(sorted(_RUNTIME_SELECTION_VALUES))
            raise ValueError(f"runtime_selection must be one of: {allowed}")
        return normalized


class ModelCodexRuntimeRequestAdapterResponse(BaseModel):
    """Structured response returned by the Codex runtime request adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    command_name: str = Field(..., min_length=1)
    command_topic: str = Field(..., min_length=1)
    response_topic: str = Field(..., min_length=1)
    correlation_id: UUID | None = None
    runtime_selection: str = Field(default="deployed")
    runtime_mode: str | None = None
    runtime_evidence: ModelRuntimeEvidence | None = None
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
    runtime_selection: str | None = None,
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
            "runtime_selection": (
                runtime_selection
                if runtime_selection is not None
                else default_runtime_selection()
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


def _deployed_runtime_evidence(
    *,
    command_topic: str,
    terminal_topic: str,
    runtime_selection: str,
) -> ModelRuntimeEvidence:
    return ModelRuntimeEvidence(
        selected_runtime_mode="deployed",
        event_bus_backend="kafka",
        state_store_backend="deployed_runtime",
        command_topic=command_topic,
        terminal_topic=terminal_topic,
        details={"runtime_selection": runtime_selection},
    )


def _local_runtime_evidence(evidence: BaseModel) -> ModelRuntimeEvidence:
    raw = cast(
        dict[str, object],
        evidence.model_dump(mode="json", exclude_none=True),
    )
    details = dict(raw)
    return ModelRuntimeEvidence(
        selected_runtime_mode=str(raw["selected_runtime_mode"]),
        event_bus_backend=str(raw["event_bus_backend"]),
        state_store_backend=str(raw["state_store_backend"]),
        node_contract=cast(str, raw.get("node_contract")),
        command_topic=str(raw["command_topic"]),
        terminal_topic=cast(str | None, raw.get("terminal_topic")),
        details=details,
    )


def _uses_direct_delegate_skill_contract(
    route: ModelDispatchBusRoute,
    command: ModelDispatchBusCommand,
) -> bool:
    return (
        route.command_topic == _DELEGATE_SKILL_COMMAND_TOPIC
        and command.command_name in _DELEGATE_SKILL_COMMAND_NAMES
    )


def _node_name_for_command(command_name: str) -> str:
    if command_name in {"delegate_skill", "delegate_skill.orchestrate"}:
        return "node_delegate_skill_orchestrator"
    candidate = command_name.split(".", 1)[0].replace("-", "_")
    if candidate.startswith("node_"):
        return candidate
    return f"node_{candidate}"


def _contract_terminal_topics(command_name: str) -> tuple[str, tuple[str, ...]] | None:
    contract_path = _NODE_ROOT / _node_name_for_command(command_name) / "contract.yaml"
    if not contract_path.exists():
        return None
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return None

    runtime_dispatch = cast(dict[str, Any], raw.get("runtime_dispatch") or {})
    terminals = cast(
        dict[str, Any],
        runtime_dispatch.get("terminal_events") or raw.get("terminal_events") or {},
    )
    terminal_topic = raw.get("terminal_event") or terminals.get("success")
    if not isinstance(terminal_topic, str) or not terminal_topic.strip():
        return None

    additional_topics: list[str] = []
    failure_topic = terminals.get("failure")
    if isinstance(failure_topic, str) and failure_topic.strip():
        additional_topics.append(failure_topic.strip())

    return terminal_topic.strip(), tuple(additional_topics)


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
        pass

    try:
        raw = json.loads(value.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    coerced = _coerce_terminal_result_payload(raw)
    if coerced is not None:
        try:
            return ModelDispatchBusTerminalResult.model_validate(coerced)
        except ValidationError:
            pass
    raw_payload = raw.get("payload")
    if not isinstance(raw_payload, dict):
        return None
    coerced = _coerce_terminal_result_payload(raw_payload)
    if coerced is None:
        return None
    try:
        return ModelDispatchBusTerminalResult.model_validate(coerced)
    except ValidationError:
        return None


def _coerce_terminal_result_payload(
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    result = dict(payload)
    if isinstance(result.get("completed_at"), str):
        result.pop("completed_at", None)
    if "result" in result and "payload" not in result:
        result["payload"] = result.pop("result")
    if result.get("status") == "timed_out":
        result["status"] = "timeout"
    if not isinstance(result.get("correlation_id"), str):
        return None
    if not isinstance(result.get("status"), str):
        return None
    return result


async def _await_terminal_subscriptions_ready(
    transport: _ProtocolLifecycleTransport,
    *,
    timeout_seconds: float,
) -> None:
    readiness = getattr(transport, "get_readiness_status", None)
    if not callable(readiness):
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_seconds)
    while True:
        status = await readiness()
        if bool(getattr(status, "is_ready", False)):
            return
        if loop.time() >= deadline:
            return
        await asyncio.sleep(_TERMINAL_SUBSCRIPTION_READY_POLL_SECONDS)


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
        runtime_selection: str | None = None,
    ) -> ModelCodexRuntimeRequestAdapterResponse:
        """Compile the adapter command envelope without touching the event bus."""
        request = _build_client_request(
            command_name=command_name,
            payload=payload,
            correlation_id=correlation_id,
            timeout_ms=timeout_ms,
            response_topic=response_topic,
            target_runtime_address=target_runtime_address,
            runtime_selection=runtime_selection,
        )
        command = _build_dispatch_command(request, requester=self._requester)
        compiled_command = command.model_dump(mode="json", exclude_none=True)
        return ModelCodexRuntimeRequestAdapterResponse(
            ok=True,
            command_name=request.command_name,
            command_topic=self._command_topic,
            response_topic=request.response_topic,
            correlation_id=command.correlation_id,
            runtime_selection=request.runtime_selection,
            runtime_mode="compiled",
            dispatch_result={
                "status": "compiled",
                "runtime_selection": request.runtime_selection,
                "command_topic": self._command_topic,
                "response_topic": request.response_topic,
                "command": compiled_command,
            },
            output_payloads=[
                {
                    "status": "compiled",
                    "runtime_selection": request.runtime_selection,
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
        runtime_selection: str | None = None,
    ) -> ModelCodexRuntimeRequestAdapterResponse:
        request = _build_client_request(
            command_name=command_name,
            payload=payload,
            correlation_id=correlation_id,
            timeout_ms=timeout_ms,
            response_topic=response_topic,
            target_runtime_address=target_runtime_address,
            runtime_selection=runtime_selection,
        )

        if request.runtime_selection == "local":
            return await self._dispatch_local_async(request)

        try:
            return await self._dispatch_deployed_async(
                request,
                additional_response_topics=additional_response_topics,
            )
        except Exception as exc:
            if request.runtime_selection != "deployed_or_local":
                raise
            return await self._dispatch_local_async(request, fallback_reason=str(exc))

    async def _dispatch_deployed_async(
        self,
        request: ModelCodexRuntimeRequestAdapterRequest,
        *,
        additional_response_topics: tuple[str, ...],
    ) -> ModelCodexRuntimeRequestAdapterResponse:

        transport = self._event_bus_factory()
        await transport.start()
        try:
            dispatch_adapter = CodexDispatchBusAdapter(
                transport, source=self._requester
            )
            command = _build_dispatch_command(request, requester=self._requester)
            # For delegate-skill commands the runtime publishes terminal results to
            # the contract-declared topics (delegate-skill-completed/failed), not to
            # the generic pattern-b-dispatch-completed topic stored in
            # request.response_topic.  Subscribing to the wrong topic causes the
            # client to never receive the reply (OMN-11991).
            is_delegate_skill = (
                self._command_topic == _DELEGATE_SKILL_COMMAND_TOPIC
                and request.command_name in _DELEGATE_SKILL_COMMAND_NAMES
            )
            if is_delegate_skill:
                terminal_topic = _DELEGATE_SKILL_COMPLETED_TOPIC
                extra_topics = (
                    _DELEGATE_SKILL_FAILED_TOPIC,
                    *additional_response_topics,
                )
            else:
                contract_topics = (
                    _contract_terminal_topics(request.command_name)
                    if request.response_topic == default_response_topic()
                    else None
                )
                if contract_topics is None:
                    terminal_topic = request.response_topic
                    extra_topics = additional_response_topics
                else:
                    terminal_topic, contract_extra_topics = contract_topics
                    extra_topics = (*contract_extra_topics, *additional_response_topics)
            route = ModelDispatchBusRoute(
                contract_path=Path("codex-runtime-request-adapter"),
                command_topic=self._command_topic,
                terminal_topic=terminal_topic,
            )
            unsubscribe, result_queue = await dispatch_adapter.wait_for_result(
                route,
                correlation_id=str(command.correlation_id),
                additional_terminal_topics=extra_topics,
                readiness_timeout_seconds=min(
                    _TERMINAL_SUBSCRIPTION_READY_TIMEOUT_SECONDS,
                    command.timeout_seconds,
                ),
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

        evidence = _deployed_runtime_evidence(
            command_topic=self._command_topic,
            terminal_topic=terminal_topic,
            runtime_selection=request.runtime_selection,
        )
        ok = terminal_result.status == "completed"
        if ok:
            return ModelCodexRuntimeRequestAdapterResponse(
                ok=True,
                command_name=request.command_name,
                command_topic=self._command_topic,
                response_topic=request.response_topic,
                correlation_id=terminal_result.correlation_id,
                runtime_selection=request.runtime_selection,
                runtime_mode="deployed",
                runtime_evidence=evidence,
                dispatch_result=_dispatch_result(terminal_result),
                output_payloads=_output_payloads(terminal_result.payload),
            )
        return ModelCodexRuntimeRequestAdapterResponse(
            ok=False,
            command_name=request.command_name,
            command_topic=self._command_topic,
            response_topic=request.response_topic,
            correlation_id=terminal_result.correlation_id,
            runtime_selection=request.runtime_selection,
            runtime_mode="deployed",
            runtime_evidence=evidence,
            dispatch_result=_dispatch_result(terminal_result),
            error=ModelCodexRuntimeRequestAdapterError(
                code=f"runtime_{terminal_result.status}",
                message=terminal_result.error_message
                or "Codex runtime adapter request did not complete successfully.",
                retryable=terminal_result.status == "timeout",
            ),
        )

    async def _dispatch_local_async(
        self,
        request: ModelCodexRuntimeRequestAdapterRequest,
        *,
        fallback_reason: str | None = None,
    ) -> ModelCodexRuntimeRequestAdapterResponse:
        from omnimarket.adapters.codex.local_runtime_dispatch import (
            LocalRuntimeDispatch,
        )

        command = _build_dispatch_command(request, requester=self._requester)
        terminal_result, evidence_raw = await LocalRuntimeDispatch(
            adapter_command_topic=self._command_topic,
            fallback_reason=fallback_reason,
        ).dispatch(command)
        evidence = _local_runtime_evidence(evidence_raw)
        ok = terminal_result.status == "completed"
        if ok:
            return ModelCodexRuntimeRequestAdapterResponse(
                ok=True,
                command_name=request.command_name,
                command_topic=self._command_topic,
                response_topic=request.response_topic,
                correlation_id=terminal_result.correlation_id,
                runtime_selection=request.runtime_selection,
                runtime_mode="local",
                runtime_evidence=evidence,
                dispatch_result=_dispatch_result(terminal_result),
                output_payloads=_output_payloads(terminal_result.payload),
            )
        return ModelCodexRuntimeRequestAdapterResponse(
            ok=False,
            command_name=request.command_name,
            command_topic=self._command_topic,
            response_topic=request.response_topic,
            correlation_id=terminal_result.correlation_id,
            runtime_selection=request.runtime_selection,
            runtime_mode="local",
            runtime_evidence=evidence,
            dispatch_result=_dispatch_result(terminal_result),
            error=ModelCodexRuntimeRequestAdapterError(
                code=f"runtime_{terminal_result.status}",
                message=terminal_result.error_message
                or "Local runtime adapter request did not complete successfully.",
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
        runtime_selection: str | None = None,
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
                runtime_selection=runtime_selection,
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
        "--runtime-selection",
        default=default_runtime_selection(),
        choices=sorted(_RUNTIME_SELECTION_VALUES),
        help=(
            "Explicit runtime selection: deployed, local, or deployed_or_local. "
            "Only deployed_or_local falls back when the deployed runtime is unavailable."
        ),
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
                runtime_selection=args.runtime_selection,
            )
        else:
            response = client.dispatch_sync(
                command_name=args.command_name,
                payload=payload,
                correlation_id=correlation_id,
                timeout_ms=args.timeout_ms,
                response_topic=args.response_topic,
                target_runtime_address=args.target_runtime_address,
                runtime_selection=args.runtime_selection,
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
    # `python -m` executes this file as `__main__`; re-enter through the
    # canonical module so generic event envelopes use one ModelDispatchBusCommand
    # class identity across the CLI and local runtime dispatcher.
    from omnimarket.adapters.codex.runtime_client import main as _canonical_main

    raise SystemExit(_canonical_main())


__all__ = [
    "CodexRuntimeRequestAdapter",
    "LocalRuntimeIngressClient",
    "ModelCodexRuntimeRequestAdapterError",
    "ModelCodexRuntimeRequestAdapterRequest",
    "ModelCodexRuntimeRequestAdapterResponse",
    "default_command_topic",
    "default_requester",
    "default_response_topic",
    "default_runtime_selection",
    "default_target_runtime_address",
    "main",
]
