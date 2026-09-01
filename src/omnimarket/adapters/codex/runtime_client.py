# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Codex runtime request adapter for Codex-facing OmniMarket skills."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumInfraTransportType
from omnibase_infra.errors import ModelInfraErrorContext, ProtocolConfigurationError
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_from_env
from omnibase_infra.runtime.overlay.contract_env_ref import expand_contract_env_refs
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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
# Contract-declared config references (OMN-13557 Wave-2 config -> overlay).
# These topic/requester/api-version tunables resolve through the sanctioned
# overlay boundary (``expand_contract_env_refs`` — the one env-reading surface)
# instead of scattered direct ``os.environ`` reads. An unbound overlay var
# expands to the empty string, in which case the canonical contract default
# constant applies (topic/requester config legitimately carries a default,
# unlike a fail-closed endpoint).
_COMMAND_TOPIC_CONTRACT_REF = "${env.ONEX_PATTERN_B_COMMAND_TOPIC}"
_RESPONSE_TOPIC_CONTRACT_REF = "${env.ONEX_PATTERN_B_RESPONSE_TOPIC}"
_REQUESTER_CONTRACT_REF = "${env.ONEX_PATTERN_B_REQUESTER}"
_API_VERSION_CONTRACT_REF = "${env.KAFKA_API_VERSION}"
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
_TERMINAL_SUBSCRIPTION_READY_TIMEOUT_SECONDS = 60.0
_TERMINAL_SUBSCRIPTION_READY_POLL_SECONDS = 0.05
_NODE_ROOT = Path(__file__).resolve().parents[2] / "nodes"


class ModelDispatchBusRoute(BaseModel):
    """Wire shape for the Codex adapter dispatch route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_path: Path
    command_topic: str
    terminal_topic: str
    event_type: str | None = None
    payload_model: str | None = None


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


class ModelRuntimeObservation(BaseModel):
    """States whether this response observed a runtime execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["OBSERVED", "UNOBSERVED"]
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_truth(self) -> ModelRuntimeObservation:
        if self.status == "OBSERVED" and self.reason != "terminal_received":
            raise ValueError("OBSERVED requires reason='terminal_received'")
        if self.status == "UNOBSERVED" and self.reason not in {
            "compile_only",
            "readiness_timeout",
            "readiness_failure",
            "publish_failure",
            "terminal_timeout",
            "local_dispatch_failure",
            "request_invalid",
        }:
            raise ValueError("UNOBSERVED requires a known non-terminal reason")
        return self


class ModelNodeContractBinding(BaseModel):
    """Typed adapter-local resolution of a target node contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["RESOLVED", "UNRESOLVABLE"]
    node_name: str
    contract_identity: str
    contract_path: str | None = None
    command_topic: str | None = None
    terminal_topic: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_resolution(self) -> ModelNodeContractBinding:
        resolved_values = (self.contract_path, self.command_topic, self.terminal_topic)
        if self.status == "RESOLVED":
            if self.reason is not None or any(
                value is None for value in resolved_values
            ):
                raise ValueError("RESOLVED requires path/topics and forbids reason")
        elif self.reason is None or any(value is not None for value in resolved_values):
            raise ValueError("UNRESOLVABLE requires reason and forbids path/topics")
        return self


class ModelAdapterDispatchBinding(BaseModel):
    """Adapter-local terminal-selection binding; not deployed-manifest proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter_command_topic: str
    requested_response_topic: str
    selected_terminal_topic: str
    terminal_selection: Literal[
        "NODE_CONTRACT",
        "DIRECT_DELEGATE_SKILL_CONTRACT",
        "EXPLICIT_RESPONSE_OVERRIDE",
        "UNRESOLVED_DEFAULT_FALLBACK",
    ]
    node_contract: ModelNodeContractBinding
    evidence_scope: Literal["adapter_local_contract_binding"] = (
        "adapter_local_contract_binding"
    )
    remote_deployed_manifest_proven: bool = False

    @model_validator(mode="after")
    def _validate_selection(self) -> ModelAdapterDispatchBinding:
        if self.evidence_scope != "adapter_local_contract_binding":
            raise ValueError("adapter binding must declare its local evidence scope")
        if self.remote_deployed_manifest_proven:
            raise ValueError("adapter-local binding cannot prove a deployed manifest")
        if self.terminal_selection == "EXPLICIT_RESPONSE_OVERRIDE":
            if self.selected_terminal_topic != self.requested_response_topic:
                raise ValueError(
                    "explicit response override must select the requested topic"
                )
        elif self.terminal_selection == "UNRESOLVED_DEFAULT_FALLBACK":
            if (
                self.node_contract.status != "UNRESOLVABLE"
                or self.selected_terminal_topic != self.requested_response_topic
                or self.requested_response_topic != default_response_topic()
            ):
                raise ValueError(
                    "unresolved fallback requires an unresolvable contract"
                )
        elif self.node_contract.status != "RESOLVED":
            raise ValueError("contract terminal selection requires a resolved contract")
        elif self.terminal_selection == "NODE_CONTRACT" and (
            self.requested_response_topic != default_response_topic()
            or self.selected_terminal_topic != self.node_contract.terminal_topic
        ):
            raise ValueError("node contract selection must use its terminal topic")
        elif self.terminal_selection == "DIRECT_DELEGATE_SKILL_CONTRACT" and (
            self.adapter_command_topic != _DELEGATE_SKILL_COMMAND_TOPIC
            or self.selected_terminal_topic != _DELEGATE_SKILL_COMPLETED_TOPIC
            or self.node_contract.node_name != "node_delegate_skill_orchestrator"
        ):
            raise ValueError("direct delegate selection must use its completed topic")
        return self


class ModelRuntimeEvidence(BaseModel):
    """Runtime/backend evidence, schema v2.

    Migration from v1: the nullable scalar ``node_contract`` moved to
    ``adapter_dispatch_binding.node_contract`` and ``runtime_observation`` is
    required. Consumers must branch on the typed binding/observation fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal["runtime-evidence/v2"]
    selected_runtime_mode: str
    event_bus_backend: str
    state_store_backend: str
    runtime_observation: ModelRuntimeObservation
    adapter_dispatch_binding: ModelAdapterDispatchBinding
    command_topic: str
    terminal_topic: str
    details: dict[str, object] = Field(default_factory=dict)


@dataclass(frozen=True)
class _ContractDispatchRoute:
    contract_path: Path
    command_topic: str
    terminal_topic: str
    additional_terminal_topics: tuple[str, ...]
    event_type: str | None
    payload_model: str | None


def default_command_topic() -> str:
    """Resolve the Codex adapter command topic via the overlay seam.

    Routes the ``ONEX_PATTERN_B_COMMAND_TOPIC`` config read through the
    sanctioned overlay boundary (the one env-reading surface). An unbound
    overlay var expands to the empty string, in which case the canonical
    contract default applies.
    """
    resolved = expand_contract_env_refs(_COMMAND_TOPIC_CONTRACT_REF)
    return resolved or _DEFAULT_COMMAND_TOPIC


def default_response_topic() -> str:
    """Resolve the Codex adapter response topic via the overlay seam.

    Routes the ``ONEX_PATTERN_B_RESPONSE_TOPIC`` config read through the
    sanctioned overlay boundary; an unbound overlay var falls back to the
    canonical contract default.
    """
    resolved = expand_contract_env_refs(_RESPONSE_TOPIC_CONTRACT_REF)
    return resolved or _DEFAULT_RESPONSE_TOPIC


def default_requester() -> str:
    """Resolve the requester label attached to adapter commands via the overlay seam.

    Routes the ``ONEX_PATTERN_B_REQUESTER`` config read through the sanctioned
    overlay boundary; an unbound overlay var falls back to the canonical
    contract default.
    """
    resolved = expand_contract_env_refs(_REQUESTER_CONTRACT_REF)
    return resolved or _DEFAULT_REQUESTER


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


def _direct_kafka_client_version_kwargs(
    transport: _ProtocolLifecycleTransport,
) -> dict[str, object]:
    config = getattr(transport, "_config", None)
    api_version = getattr(config, "api_version", None)
    if isinstance(api_version, str) and api_version.strip():
        return {"api_version": api_version.strip()}

    # Resolve the KAFKA_API_VERSION config tunable through the sanctioned overlay
    # boundary instead of a direct os.environ read (OMN-13557 Wave-2). An unbound
    # overlay var expands to the empty string -> no api_version kwarg.
    overlay_value = expand_contract_env_refs(_API_VERSION_CONTRACT_REF)
    if overlay_value.strip():
        return {"api_version": overlay_value.strip()}

    return {}


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
        topics: list[str] = [route.terminal_topic]
        for extra in additional_terminal_topics:
            if extra and extra not in topics:
                topics.append(extra)

        try:
            direct_listener = await self._try_direct_kafka_terminal_listener(
                topics=tuple(topics),
                correlation_id=correlation_id,
                queue=q,
                timeout_seconds=readiness_timeout_seconds,
            )
        except Exception:
            direct_listener = None
        if direct_listener is not None:
            return direct_listener

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

        async def _unsubscribe() -> None:
            for unsub in unsubs:
                if callable(unsub):
                    try:
                        result = unsub()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass

        try:
            await _await_terminal_subscriptions_ready(
                self._transport,
                timeout_seconds=readiness_timeout_seconds,
            )
        except TimeoutError:
            await _unsubscribe()
            raise

        return _unsubscribe, q

    async def _try_direct_kafka_terminal_listener(
        self,
        *,
        topics: tuple[str, ...],
        correlation_id: str,
        queue: asyncio.Queue[ModelDispatchBusTerminalResult],
        timeout_seconds: float,
    ) -> (
        tuple[
            Callable[[], Awaitable[None]],
            asyncio.Queue[ModelDispatchBusTerminalResult],
        ]
        | None
    ):
        bootstrap_servers = getattr(self._transport, "_bootstrap_servers", None)
        if not isinstance(bootstrap_servers, str) or not bootstrap_servers.strip():
            return None

        try:
            from aiokafka import AIOKafkaConsumer
        except Exception:
            return None

        consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=bootstrap_servers,
            group_id=None,
            enable_auto_commit=False,
            auto_offset_reset="latest",
            **_direct_kafka_client_version_kwargs(self._transport),
            **build_aiokafka_auth_kwargs_from_env(),
        )
        try:
            await asyncio.wait_for(consumer.start(), timeout=timeout_seconds)
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            assignment = consumer.assignment()
            while not assignment and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(_TERMINAL_SUBSCRIPTION_READY_POLL_SECONDS)
                assignment = consumer.assignment()
            if not assignment:
                raise TimeoutError("Timed out waiting for direct terminal assignment.")
            await consumer.seek_to_end(*assignment)
        except Exception:
            await consumer.stop()
            raise

        stopped = asyncio.Event()

        async def _consume() -> None:
            while not stopped.is_set():
                batches = await consumer.getmany(timeout_ms=500, max_records=100)
                for records in batches.values():
                    for msg in records:
                        value = msg.value
                        if isinstance(value, bytearray):
                            value = bytes(value)
                        if not isinstance(value, bytes):
                            continue
                        result = _parse_terminal_result(value)
                        if result is None:
                            continue
                        if str(result.correlation_id) == correlation_id:
                            await queue.put(result)
                            return

        task = asyncio.create_task(_consume())

        async def _unsubscribe() -> None:
            stopped.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                await consumer.stop()

        return _unsubscribe, queue

    async def publish_command(
        self,
        route: ModelDispatchBusRoute,
        command: ModelDispatchBusCommand,
    ) -> None:
        if _uses_direct_delegate_skill_contract(route, command):
            await self._publish_delegate_skill_command(route, command)
            return
        if route.payload_model is not None:
            await self._publish_node_contract_command(route, command)
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

    async def _publish_node_contract_command(
        self,
        route: ModelDispatchBusRoute,
        command: ModelDispatchBusCommand,
    ) -> None:
        if route.payload_model is None:
            raise ValueError("node contract route requires a payload_model")

        module_name, model_name = route.payload_model.rsplit(".", 1)
        payload_model = getattr(importlib.import_module(module_name), model_name)
        payload = _payload_with_command_correlation_id(
            payload_model,
            command.payload,
            command.correlation_id,
        )
        typed_payload = payload_model.model_validate(payload)
        envelope = ModelEventEnvelope[Any](
            payload=typed_payload,
            correlation_id=command.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=route.event_type or route.command_topic,
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


_KAFKA_BOOTSTRAP_ENV_VAR = "KAFKA_BOOTSTRAP_SERVERS"


def _check_bus_target_configured() -> None:
    """Fail fast with a typed error if no Kafka bus target is configured.

    When KAFKA_BOOTSTRAP_SERVERS is absent the EventBusKafka.default()
    factory silently falls back to localhost:19092.  That fallback enters a
    30-second aiokafka connection-retry loop before raising InfraTimeoutError,
    which is indistinguishable from a real runtime failure.

    This guard fires *before* the producer bootstrap loop so that an
    unconfigured overnight sweep produces a typed blocker packet
    (ProtocolConfigurationError naming the missing env var) instead of a
    30-second hang followed by an opaque timeout.

    Raises:
        ProtocolConfigurationError: If KAFKA_BOOTSTRAP_SERVERS is unset or
            empty, naming the missing configuration target explicitly.
    """
    value = os.environ.get(_KAFKA_BOOTSTRAP_ENV_VAR)
    if value is None or not value.strip():
        context = ModelInfraErrorContext.with_correlation(
            transport_type=EnumInfraTransportType.KAFKA,
            operation="deployed_event_bus_factory",
            target_name="codex_runtime_request_adapter",
        )
        raise ProtocolConfigurationError(
            f"Deployed bus target is not configured: "
            f"environment variable {_KAFKA_BOOTSTRAP_ENV_VAR!r} is missing or empty. "
            f"Set {_KAFKA_BOOTSTRAP_ENV_VAR} to the Redpanda/Kafka bootstrap address "
            f"(e.g. '<host>:19092'). "
            f"Use --runtime-selection local or --compile-only to run without a live bus.",
            context=context,
            parameter=_KAFKA_BOOTSTRAP_ENV_VAR,
            value=value,
        )


def _default_event_bus_factory() -> _ProtocolLifecycleTransport:
    _check_bus_target_configured()
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


def _is_success_status(status: str) -> bool:
    return status in {"complete", "completed"}


def _dispatch_result(
    result: ModelDispatchBusTerminalResult,
) -> dict[str, object]:
    return cast(dict[str, object], result.model_dump(mode="json", exclude_none=True))


def _deployed_runtime_evidence(
    *,
    binding: ModelAdapterDispatchBinding,
    command_topic: str,
    runtime_selection: str,
    observation: ModelRuntimeObservation,
) -> ModelRuntimeEvidence:
    return ModelRuntimeEvidence(
        schema_version="runtime-evidence/v2",
        selected_runtime_mode="deployed",
        event_bus_backend="kafka",
        state_store_backend="deployed_runtime",
        runtime_observation=observation,
        adapter_dispatch_binding=binding,
        command_topic=command_topic,
        terminal_topic=binding.selected_terminal_topic,
        details={
            "runtime_selection": runtime_selection,
            "evidence_scope": "adapter_local_contract_binding",
            "remote_deployed_manifest_proven": False,
        },
    )


def _local_runtime_evidence(evidence: BaseModel) -> ModelRuntimeEvidence:
    raw = cast(
        dict[str, object],
        evidence.model_dump(mode="json", exclude_none=True),
    )
    details = dict(raw)
    return ModelRuntimeEvidence(
        schema_version="runtime-evidence/v2",
        selected_runtime_mode=str(raw["selected_runtime_mode"]),
        event_bus_backend=str(raw["event_bus_backend"]),
        state_store_backend=str(raw["state_store_backend"]),
        runtime_observation=ModelRuntimeObservation.model_validate(
            raw["runtime_observation"]
        ),
        adapter_dispatch_binding=ModelAdapterDispatchBinding.model_validate(
            raw["adapter_dispatch_binding"]
        ),
        command_topic=str(raw["command_topic"]),
        terminal_topic=str(raw["terminal_topic"]),
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


def _contract_dispatch_route(command_name: str) -> _ContractDispatchRoute | None:
    contract_path = _NODE_ROOT / _node_name_for_command(command_name) / "contract.yaml"
    if not contract_path.exists():
        return None
    try:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    if not _has_valid_contract_shapes(raw):
        return None

    event_bus_raw = raw.get("event_bus", {})
    runtime_dispatch_raw = raw.get("runtime_dispatch", {})
    if not isinstance(event_bus_raw, dict) or not isinstance(
        runtime_dispatch_raw, dict
    ):
        return None
    event_bus = cast(dict[str, Any], event_bus_raw)
    runtime_dispatch = cast(dict[str, Any], runtime_dispatch_raw)
    runtime_terminals = runtime_dispatch.get("terminal_events")
    terminals_raw = (
        runtime_terminals
        if runtime_terminals is not None
        else raw.get("terminal_events", {})
    )
    if not isinstance(terminals_raw, dict):
        return None
    terminals = cast(dict[str, Any], terminals_raw)
    command_topic = runtime_dispatch.get("command_topic") or _first_topic(
        event_bus.get("subscribe_topics"),
        event_bus.get("subscribe"),
        raw.get("subscribe_topics"),
        raw.get("subscribe_topic"),
    )
    if not isinstance(command_topic, str) or not command_topic.strip():
        return None

    terminal_topic = (
        raw.get("terminal_event")
        or terminals.get("success")
        or _completed_topic(
            event_bus.get("publish_topics"),
            event_bus.get("publish"),
            raw.get("publish_topics"),
            raw.get("publish_topic"),
        )
    )
    if not isinstance(terminal_topic, str) or not terminal_topic.strip():
        return None

    additional_topics: list[str] = []
    failure_topic = terminals.get("failure")
    if not isinstance(failure_topic, str) or not failure_topic.strip():
        failure_topic = _first_topic_containing(
            event_bus.get("publish_topics"), "failed"
        ) or _topic_from_value(
            event_bus.get("publish"), mapping_keys=("failure_topic",)
        )
    if isinstance(failure_topic, str) and failure_topic.strip():
        additional_topics.append(failure_topic.strip())

    payload_model = _input_model_ref(
        raw, node_name=_node_name_for_command(command_name)
    )
    event_type = _handler_event_type(raw)

    return _ContractDispatchRoute(
        contract_path=contract_path,
        command_topic=command_topic.strip(),
        terminal_topic=terminal_topic.strip(),
        additional_terminal_topics=tuple(additional_topics),
        event_type=event_type,
        payload_model=payload_model,
    )


def _node_contract_binding(command_name: str) -> ModelNodeContractBinding:
    """Resolve adapter-local contract identity without collapsing failures to None."""

    node_name = _node_name_for_command(command_name)
    contract_path = _NODE_ROOT / node_name / "contract.yaml"
    contract_identity = f"src/omnimarket/nodes/{node_name}/contract.yaml"
    if not contract_path.exists():
        return ModelNodeContractBinding(
            status="UNRESOLVABLE",
            node_name=node_name,
            contract_identity=contract_identity,
            reason="missing_contract",
        )
    try:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return ModelNodeContractBinding(
            status="UNRESOLVABLE",
            node_name=node_name,
            contract_identity=contract_identity,
            reason="contract_unreadable",
        )
    except yaml.YAMLError:
        return ModelNodeContractBinding(
            status="UNRESOLVABLE",
            node_name=node_name,
            contract_identity=contract_identity,
            reason="malformed_contract",
        )
    if not isinstance(raw, dict):
        return ModelNodeContractBinding(
            status="UNRESOLVABLE",
            node_name=node_name,
            contract_identity=contract_identity,
            reason="malformed_contract",
        )
    if not _has_valid_contract_shapes(raw):
        return ModelNodeContractBinding(
            status="UNRESOLVABLE",
            node_name=node_name,
            contract_identity=contract_identity,
            reason="malformed_contract",
        )
    route = _contract_dispatch_route(command_name)
    if route is None:
        return ModelNodeContractBinding(
            status="UNRESOLVABLE",
            node_name=node_name,
            contract_identity=contract_identity,
            reason="missing_dispatch_topics",
        )
    return ModelNodeContractBinding(
        status="RESOLVED",
        node_name=node_name,
        contract_identity=contract_identity,
        contract_path=contract_identity,
        command_topic=route.command_topic,
        terminal_topic=route.terminal_topic,
    )


def _adapter_dispatch_binding(
    *,
    request: ModelCodexRuntimeRequestAdapterRequest,
    adapter_command_topic: str,
    contract_route: _ContractDispatchRoute | None,
) -> ModelAdapterDispatchBinding:
    """Describe exactly how this adapter selected its terminal subscription."""

    node_contract = _node_contract_binding(request.command_name)
    if (
        adapter_command_topic == _DELEGATE_SKILL_COMMAND_TOPIC
        and request.command_name in _DELEGATE_SKILL_COMMAND_NAMES
    ):
        return ModelAdapterDispatchBinding(
            adapter_command_topic=adapter_command_topic,
            requested_response_topic=request.response_topic,
            selected_terminal_topic=_DELEGATE_SKILL_COMPLETED_TOPIC,
            terminal_selection="DIRECT_DELEGATE_SKILL_CONTRACT",
            node_contract=node_contract,
        )
    if request.response_topic != default_response_topic():
        return ModelAdapterDispatchBinding(
            adapter_command_topic=adapter_command_topic,
            requested_response_topic=request.response_topic,
            selected_terminal_topic=request.response_topic,
            terminal_selection="EXPLICIT_RESPONSE_OVERRIDE",
            node_contract=node_contract,
        )
    if contract_route is not None:
        return ModelAdapterDispatchBinding(
            adapter_command_topic=adapter_command_topic,
            requested_response_topic=request.response_topic,
            selected_terminal_topic=contract_route.terminal_topic,
            terminal_selection="NODE_CONTRACT",
            node_contract=node_contract,
        )
    return ModelAdapterDispatchBinding(
        adapter_command_topic=adapter_command_topic,
        requested_response_topic=request.response_topic,
        selected_terminal_topic=request.response_topic,
        terminal_selection="UNRESOLVED_DEFAULT_FALLBACK",
        node_contract=node_contract,
    )


def _contract_terminal_topics(command_name: str) -> tuple[str, tuple[str, ...]] | None:
    route = _contract_dispatch_route(command_name)
    if route is None:
        return None
    return route.terminal_topic, route.additional_terminal_topics


_EVENT_BUS_SUBSCRIBE_MAPPING_KEYS = ("topic", "command_topic")
_EVENT_BUS_PUBLISH_MAPPING_KEYS = (
    "topic",
    "success_topic",
    "completed_topic",
    "failure_topic",
)


def _first_topic(*values: object) -> str | None:
    for value in values:
        topic = _first_string(value)
        if topic is not None:
            return topic
    return None


def _first_topic_containing(value: object, fragment: str) -> str | None:
    if isinstance(value, list):
        for item in value:
            topic = _topic_from_value(
                item, mapping_keys=_EVENT_BUS_PUBLISH_MAPPING_KEYS
            )
            if topic is not None and fragment in topic:
                return topic
    topic = _topic_from_value(value, mapping_keys=_EVENT_BUS_PUBLISH_MAPPING_KEYS)
    if topic is not None and fragment in topic:
        return topic
    return None


def _first_string(
    value: object, *, mapping_keys: tuple[str, ...] = _EVENT_BUS_SUBSCRIBE_MAPPING_KEYS
) -> str | None:
    topic = _topic_from_value(value, mapping_keys=mapping_keys)
    if topic is not None:
        return topic
    if isinstance(value, list):
        return next(
            (
                topic
                for item in value
                if (topic := _topic_from_value(item, mapping_keys=mapping_keys))
                is not None
            ),
            None,
        )
    return None


def _topic_from_value(
    value: object, *, mapping_keys: tuple[str, ...] = _EVENT_BUS_SUBSCRIBE_MAPPING_KEYS
) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in mapping_keys:
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


def _completed_topic(*values: object) -> str | None:
    """Resolve a terminal topic from a validated event-bus publication form."""

    completed_keys = ("success_topic", "completed_topic", "topic")
    for value in values:
        topic = _topic_from_value(value, mapping_keys=completed_keys)
        if topic is not None and ("completed" in topic or not isinstance(value, list)):
            return topic
    for value in values:
        if isinstance(value, list):
            for item in value:
                topic = _topic_from_value(item, mapping_keys=completed_keys)
                if topic is not None and "completed" in topic:
                    return topic
            topic = _first_string(value, mapping_keys=completed_keys)
            if topic is not None:
                return topic
    return next(
        (
            topic
            for value in values
            if (topic := _first_string(value, mapping_keys=completed_keys)) is not None
        ),
        None,
    )


def _valid_event_bus_topic_value(
    value: object,
    *,
    mapping_keys: tuple[str, ...],
    required_mapping_keys: tuple[str, ...],
) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, dict) or not value:
        return False
    return (
        set(value).issubset(mapping_keys)
        and bool(set(value).intersection(required_mapping_keys))
        and all(
            isinstance(topic, str) and bool(topic.strip()) for topic in value.values()
        )
    )


def _has_valid_contract_shapes(contract: dict[str, Any]) -> bool:
    """Reject malformed nested dispatch metadata before any mapping access."""

    for key in ("event_bus", "runtime_dispatch", "terminal_events", "handler"):
        value = contract.get(key)
        if value is not None and not isinstance(value, dict):
            return False
    routing = contract.get("handler_routing")
    if routing is not None:
        if not isinstance(routing, dict):
            return False
        handlers = routing.get("handlers")
        if handlers is not None and (
            not isinstance(handlers, list)
            or any(not isinstance(handler, dict) for handler in handlers)
        ):
            return False
        if isinstance(handlers, list):
            for handler_entry in handlers:
                for key in (
                    "handler_module",
                    "handler_class",
                    "module",
                    "class",
                    "name",
                    "event_type",
                ):
                    value = handler_entry.get(key)
                    if value is not None and (
                        not isinstance(value, str) or not value.strip()
                    ):
                        return False
                nested = handler_entry.get("handler")
                if nested is not None and not _has_valid_contract_shapes(
                    {"handler": nested}
                ):
                    return False
                for key in ("input_model", "event_model"):
                    value = handler_entry.get(key)
                    if value is not None and not _valid_model_reference(value):
                        return False
    event_bus = contract.get("event_bus")
    if isinstance(event_bus, dict):
        for key in ("subscribe_topics", "publish_topics"):
            value = event_bus.get(key)
            if value is None:
                continue
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                return False
        for key, mapping_keys, required_mapping_keys in (
            (
                "subscribe",
                _EVENT_BUS_SUBSCRIBE_MAPPING_KEYS,
                _EVENT_BUS_SUBSCRIBE_MAPPING_KEYS,
            ),
            (
                "publish",
                _EVENT_BUS_PUBLISH_MAPPING_KEYS,
                ("topic", "success_topic", "completed_topic"),
            ),
        ):
            value = event_bus.get(key)
            if value is not None and not _valid_event_bus_topic_value(
                value,
                mapping_keys=mapping_keys,
                required_mapping_keys=required_mapping_keys,
            ):
                return False
    runtime_dispatch = contract.get("runtime_dispatch")
    if isinstance(runtime_dispatch, dict):
        command_topic = runtime_dispatch.get("command_topic")
        if command_topic is not None and (
            not isinstance(command_topic, str) or not command_topic.strip()
        ):
            return False
        terminals = runtime_dispatch.get("terminal_events")
        if terminals is not None and (
            not isinstance(terminals, dict)
            or any(
                not isinstance(value, str) or not value.strip()
                for value in terminals.values()
            )
        ):
            return False
    top_terminals = contract.get("terminal_events")
    if top_terminals is not None and (
        not isinstance(top_terminals, dict)
        or any(
            not isinstance(value, str) or not value.strip()
            for value in top_terminals.values()
        )
    ):
        return False
    for key in ("terminal_event", "subscribe_topic", "publish_topic"):
        value = contract.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return False
    handler = contract.get("handler")
    if isinstance(handler, dict):
        for key in ("module", "class", "name"):
            value = handler.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                return False
        input_model = handler.get("input_model")
        if input_model is not None and not _valid_model_reference(input_model):
            return False
    models = contract.get("models")
    if models is not None:
        if not isinstance(models, dict):
            return False
        model_input = models.get("input")
        if model_input is not None and not _valid_model_reference(model_input):
            return False
    return True


def _valid_model_reference(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, dict):
        return False
    for key in ("module", "class", "name"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not item.strip()):
            return False
    return bool(value.get("module") and (value.get("class") or value.get("name")))


def _input_model_ref(contract: dict[str, Any], *, node_name: str) -> str | None:
    handler = cast(dict[str, Any], contract.get("handler") or {})
    raw_model = handler.get("input_model") or contract.get("input_model")
    resolved = _model_ref_from_value(raw_model, node_name=node_name)
    if resolved is not None:
        return resolved

    routing = cast(dict[str, Any], contract.get("handler_routing") or {})
    handlers = routing.get("handlers")
    if not isinstance(handlers, list):
        return None
    for raw_handler in handlers:
        if not isinstance(raw_handler, dict):
            continue
        for key in ("input_model", "event_model"):
            resolved = _model_ref_from_value(raw_handler.get(key), node_name=node_name)
            if resolved is not None:
                return resolved
    return None


def _model_ref_from_value(value: object, *, node_name: str) -> str | None:
    if isinstance(value, str):
        if "." in value:
            return value
        return _model_ref_from_local_name(node_name, value)
    if isinstance(value, dict):
        module = value.get("module")
        name = value.get("class") or value.get("name")
        if isinstance(module, str) and isinstance(name, str) and module and name:
            if module.endswith(f".{name}"):
                return module
            if "." in module:
                return f"{module}.{name}"
            return _model_ref_from_local_name(node_name, name)
    return None


def _model_ref_from_local_name(node_name: str, model_name: str) -> str | None:
    if "." in model_name:
        return model_name
    node_dir = _NODE_ROOT / node_name / "models"
    if not node_dir.is_dir():
        return None
    for model_file in sorted(node_dir.glob("*.py")):
        if model_file.name == "__init__.py":
            continue
        try:
            source = model_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if f"class {model_name}" in source:
            return f"omnimarket.nodes.{node_name}.models.{model_file.stem}.{model_name}"

    init_file = node_dir / "__init__.py"
    try:
        init_source = init_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if model_name in init_source:
        return f"omnimarket.nodes.{node_name}.models.{model_name}"
    return None


def _handler_event_type(contract: dict[str, Any]) -> str | None:
    routing = cast(dict[str, Any], contract.get("handler_routing") or {})
    handlers = routing.get("handlers")
    if not isinstance(handlers, list):
        return None
    for raw_handler in handlers:
        if not isinstance(raw_handler, dict):
            continue
        event_type = raw_handler.get("event_type")
        if isinstance(event_type, str) and event_type.strip():
            return event_type.strip()
    return None


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
        if envelope.payload.payload is not None or not _is_success_status(
            envelope.payload.status
        ):
            return envelope.payload
    except Exception:
        pass

    try:
        raw = json.loads(value.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    event_type = raw.get("event_type")
    event_type_str = event_type if isinstance(event_type, str) else None
    raw_payload = raw.get("payload")
    if (
        isinstance(raw_payload, dict)
        and event_type_str is not None
        and isinstance(raw_payload.get("correlation_id"), str)
    ):
        coerced_payload = _coerce_terminal_result_payload(
            raw_payload, event_type=event_type_str
        )
        if coerced_payload is not None:
            try:
                return ModelDispatchBusTerminalResult.model_validate(coerced_payload)
            except ValidationError:
                pass

    coerced = _coerce_terminal_result_payload(raw, event_type=event_type_str)
    if coerced is not None:
        try:
            return ModelDispatchBusTerminalResult.model_validate(coerced)
        except ValidationError:
            pass
    if not isinstance(raw_payload, dict):
        return None
    coerced = _coerce_terminal_result_payload(raw_payload, event_type=event_type_str)
    if coerced is None:
        return None
    try:
        return ModelDispatchBusTerminalResult.model_validate(coerced)
    except ValidationError:
        return None


def _coerce_terminal_result_payload(
    payload: dict[str, Any],
    *,
    event_type: str | None = None,
) -> dict[str, Any] | None:
    result = dict(payload)
    if isinstance(result.get("completed_at"), str):
        result.pop("completed_at", None)
    if "result" in result and "payload" not in result:
        result["payload"] = result.pop("result")
    if not isinstance(result.get("status"), str):
        if event_type is not None and "completed" in event_type:
            result["status"] = "completed"
        elif event_type is not None and "failed" in event_type:
            result["status"] = "failed"
    if result.get("status") == "timed_out":
        result["status"] = "timeout"
    if not isinstance(result.get("correlation_id"), str):
        return None
    if not isinstance(result.get("status"), str):
        return None
    if "payload" not in result:
        result["payload"] = {
            key: value
            for key, value in payload.items()
            if key not in {"correlation_id", "error_message"}
        }
    return result


def _payload_with_command_correlation_id(
    input_model: Any,
    payload: dict[str, object],
    correlation_id: UUID,
) -> dict[str, object]:
    model_fields = getattr(input_model, "model_fields", {})
    if not isinstance(model_fields, dict):
        return payload
    stamped = dict(payload)
    if "correlation_id" in model_fields:
        stamped["correlation_id"] = str(correlation_id)
    return stamped


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
            raise TimeoutError("Timed out waiting for terminal subscription readiness.")
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
        command_topic = self._command_topic
        contract_route = _contract_dispatch_route(request.command_name)
        is_native_pr_lifecycle = (
            request.command_name == "pr_lifecycle_orchestrator"
            and self._command_topic == default_command_topic()
            and request.response_topic == default_response_topic()
        )
        if (
            is_native_pr_lifecycle
            and contract_route is not None
            and contract_route.payload_model is not None
        ):
            command_topic = contract_route.command_topic
        binding = _adapter_dispatch_binding(
            request=request,
            adapter_command_topic=command_topic,
            contract_route=contract_route,
        )
        compiled_command = command.model_dump(mode="json", exclude_none=True)
        compiled_dispatch = {
            "status": "compiled",
            "runtime_selection": request.runtime_selection,
            "command_topic": command_topic,
            "response_topic": request.response_topic,
            "terminal_topic": binding.selected_terminal_topic,
            "command": compiled_command,
        }
        return ModelCodexRuntimeRequestAdapterResponse(
            ok=True,
            command_name=request.command_name,
            command_topic=command_topic,
            response_topic=request.response_topic,
            correlation_id=command.correlation_id,
            runtime_selection=request.runtime_selection,
            runtime_mode="compiled",
            runtime_evidence=ModelRuntimeEvidence(
                schema_version="runtime-evidence/v2",
                selected_runtime_mode="compiled",
                event_bus_backend="unobserved",
                state_store_backend="unobserved",
                runtime_observation=ModelRuntimeObservation(
                    status="UNOBSERVED", reason="compile_only"
                ),
                adapter_dispatch_binding=binding,
                command_topic=command_topic,
                terminal_topic=binding.selected_terminal_topic,
                details={"runtime_selection": request.runtime_selection},
            ),
            dispatch_result=compiled_dispatch,
            output_payloads=[compiled_dispatch],
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
            contract_route = _contract_dispatch_route(request.command_name)
            is_native_pr_lifecycle = (
                request.command_name == "pr_lifecycle_orchestrator"
                and self._command_topic == default_command_topic()
                and request.response_topic == default_response_topic()
            )
            if is_delegate_skill:
                terminal_topic = _DELEGATE_SKILL_COMPLETED_TOPIC
                command_topic = self._command_topic
                route_event_type = None
                route_payload_model = None
                extra_topics = (
                    _DELEGATE_SKILL_FAILED_TOPIC,
                    *additional_response_topics,
                )
            elif (
                is_native_pr_lifecycle
                and contract_route is not None
                and contract_route.payload_model is not None
            ):
                terminal_topic = contract_route.terminal_topic
                command_topic = contract_route.command_topic
                route_event_type = contract_route.event_type
                route_payload_model = contract_route.payload_model
                extra_topics = (
                    *contract_route.additional_terminal_topics,
                    *additional_response_topics,
                )
            else:
                contract_topics = (
                    _contract_terminal_topics(request.command_name)
                    if request.response_topic == default_response_topic()
                    else None
                )
                command_topic = self._command_topic
                route_event_type = None
                route_payload_model = None
                if contract_topics is None:
                    terminal_topic = request.response_topic
                    extra_topics = additional_response_topics
                else:
                    terminal_topic, contract_extra_topics = contract_topics
                    extra_topics = (*contract_extra_topics, *additional_response_topics)
            binding = _adapter_dispatch_binding(
                request=request,
                adapter_command_topic=command_topic,
                contract_route=contract_route,
            )
            route = ModelDispatchBusRoute(
                contract_path=Path("codex-runtime-request-adapter"),
                command_topic=command_topic,
                terminal_topic=terminal_topic,
                event_type=route_event_type,
                payload_model=route_payload_model,
            )
            try:
                unsubscribe, result_queue = await dispatch_adapter.wait_for_result(
                    route,
                    correlation_id=str(command.correlation_id),
                    additional_terminal_topics=extra_topics,
                    readiness_timeout_seconds=min(
                        _TERMINAL_SUBSCRIPTION_READY_TIMEOUT_SECONDS,
                        command.timeout_seconds,
                    ),
                )
            except TimeoutError:
                timeout_error = handle_timeout(
                    operation="Codex runtime adapter terminal subscription readiness",
                    timeout_ms=int(
                        min(
                            _TERMINAL_SUBSCRIPTION_READY_TIMEOUT_SECONDS,
                            command.timeout_seconds,
                        )
                        * 1000
                    ),
                    correlation_id=command.correlation_id,
                )
                terminal_result = ModelDispatchBusTerminalResult(
                    correlation_id=command.correlation_id,
                    status="timeout",
                    error_message=timeout_error.message,
                )
                observation = ModelRuntimeObservation(
                    status="UNOBSERVED", reason="readiness_timeout"
                )
            except Exception as exc:
                terminal_result = ModelDispatchBusTerminalResult(
                    correlation_id=command.correlation_id,
                    status="failed",
                    error_message=str(exc),
                )
                observation = ModelRuntimeObservation(
                    status="UNOBSERVED", reason="readiness_failure"
                )
            else:
                try:
                    await dispatch_adapter.publish_command(route, command)
                except Exception as exc:
                    terminal_result = ModelDispatchBusTerminalResult(
                        correlation_id=command.correlation_id,
                        status="failed",
                        error_message=str(exc),
                    )
                    observation = ModelRuntimeObservation(
                        status="UNOBSERVED", reason="publish_failure"
                    )
                else:
                    try:
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
                        observation = ModelRuntimeObservation(
                            status="UNOBSERVED", reason="terminal_timeout"
                        )
                    else:
                        observation = ModelRuntimeObservation(
                            status="OBSERVED", reason="terminal_received"
                        )
                finally:
                    await unsubscribe()
        finally:
            await transport.close()

        evidence = _deployed_runtime_evidence(
            binding=binding,
            command_topic=command_topic,
            runtime_selection=request.runtime_selection,
            observation=observation,
        )
        ok = _is_success_status(terminal_result.status)
        if ok:
            return ModelCodexRuntimeRequestAdapterResponse(
                ok=True,
                command_name=request.command_name,
                command_topic=command_topic,
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
            command_topic=command_topic,
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
        ok = _is_success_status(terminal_result.status)
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


def _unobserved_runtime_evidence(
    *, command_name: str, reason: str
) -> ModelRuntimeEvidence:
    """Build a non-ambiguous evidence packet for requests that never ran."""

    request = _build_client_request(command_name=command_name)
    observation_reason = (
        reason
        if reason
        in {
            "compile_only",
            "readiness_timeout",
            "readiness_failure",
            "publish_failure",
            "terminal_timeout",
            "local_dispatch_failure",
        }
        else "request_invalid"
    )
    binding = _adapter_dispatch_binding(
        request=request,
        adapter_command_topic=default_command_topic(),
        contract_route=_contract_dispatch_route(command_name),
    )
    return ModelRuntimeEvidence(
        schema_version="runtime-evidence/v2",
        selected_runtime_mode="unobserved",
        event_bus_backend="unobserved",
        state_store_backend="unobserved",
        runtime_observation=ModelRuntimeObservation(
            status="UNOBSERVED", reason=observation_reason
        ),
        adapter_dispatch_binding=binding,
        command_topic=default_command_topic(),
        terminal_topic=binding.selected_terminal_topic,
    )


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
        runtime_evidence=_unobserved_runtime_evidence(
            command_name=command_name, reason=code
        ),
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
    except ProtocolConfigurationError as exc:
        response = _build_cli_error_response(
            command_name=args.command_name,
            code="bus_target_not_configured",
            message=str(exc),
            details={"missing_env_var": _KAFKA_BOOTSTRAP_ENV_VAR},
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
    "ModelAdapterDispatchBinding",
    "ModelCodexRuntimeRequestAdapterError",
    "ModelCodexRuntimeRequestAdapterRequest",
    "ModelCodexRuntimeRequestAdapterResponse",
    "ModelNodeContractBinding",
    "ModelRuntimeEvidence",
    "ModelRuntimeObservation",
    "default_command_topic",
    "default_requester",
    "default_response_topic",
    "default_runtime_selection",
    "default_target_runtime_address",
    "main",
]
