# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Local runtime dispatch for Codex-facing OmniMarket adapter requests.

This module is intentionally runtime-shaped: callers publish the same adapter
command envelope to an in-memory bus, and this local runtime worker routes it
through the node contract's command topic, payload model, handler declaration,
and terminal topic. It is not a skill-layer handler shortcut.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope
from omnibase_core.services.state.service_state_disk import ServiceStateDisk
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusCommand,
    ModelDispatchBusTerminalResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models import (
    ModelDelegationRequest,
    ModelDelegationResult,
)

_NODE_ROOT = Path(__file__).resolve().parents[2] / "nodes"
_LOCAL_RUNTIME_SOURCE = "codex-local-runtime"
_DELEGATION_REQUEST_TOPIC = ".".join(
    ("onex", "cmd", "omnibase-infra", "delegation-request", "v1")
)
_DELEGATION_COMPLETED_TOPIC = ".".join(
    ("onex", "evt", "omnibase-infra", "delegation-completed", "v1")
)


class ModelLocalRuntimeEvidence(BaseModel):
    """Observable evidence for an explicit local runtime dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_runtime_mode: str = "local"
    event_bus_backend: str = "inmemory"
    state_store_backend: str = "local_disk"
    state_root: str
    node_name: str
    node_contract: str
    adapter_command_topic: str
    command_topic: str
    terminal_topic: str
    payload_model: str
    handler_route: str
    fallback_reason: str | None = None
    events: list[dict[str, str]] = Field(default_factory=list)


@dataclass(frozen=True)
class _NodeRoute:
    node_name: str
    contract_path: Path
    command_topic: str
    terminal_topic: str
    failure_topic: str | None
    input_model_module: str
    input_model_name: str
    handler_module: str
    handler_class: str

    @property
    def payload_model_ref(self) -> str:
        return f"{self.input_model_module}.{self.input_model_name}"

    @property
    def handler_ref(self) -> str:
        return f"{self.handler_module}.{self.handler_class}"


class _LocalRuntimeStateStore(ServiceStateDisk):
    """Disk-backed local state store with a stable evidence backend label."""

    backend_label = "local_disk"


class LocalRuntimeDispatch:
    """Run adapter commands through a local in-memory runtime bus."""

    def __init__(
        self,
        *,
        adapter_command_topic: str,
        state_root: Path | None = None,
        fallback_reason: str | None = None,
    ) -> None:
        self._adapter_command_topic = adapter_command_topic
        self._bus = EventBusInmemory(environment="local", group="codex-runtime")
        root = state_root or Path(
            os.environ.get(
                "ONEX_LOCAL_RUNTIME_STATE_ROOT",
                ".onex_state/local_runtime",
            )
        )
        self._state_store = _LocalRuntimeStateStore(root)
        self._fallback_reason = fallback_reason

    async def dispatch(
        self,
        command: ModelDispatchBusCommand,
    ) -> tuple[ModelDispatchBusTerminalResult, ModelLocalRuntimeEvidence]:
        """Publish the adapter command and await the local runtime terminal."""

        await self._bus.start()
        try:
            result_queue: asyncio.Queue[
                tuple[ModelDispatchBusTerminalResult, ModelLocalRuntimeEvidence]
            ] = asyncio.Queue(maxsize=1)
            unsubscribe = await self._bus.subscribe(
                self._adapter_command_topic,
                None,
                lambda message: self._on_adapter_command(message, result_queue),
                group_id=f"codex-local-runtime-{command.correlation_id.hex}",
            )
            try:
                envelope = ModelEventEnvelope[ModelDispatchBusCommand](
                    payload=command,
                    correlation_id=command.correlation_id,
                    envelope_timestamp=datetime.now(UTC),
                    event_type=self._adapter_command_topic,
                    source_tool=_LOCAL_RUNTIME_SOURCE,
                )
                await self._bus.publish(
                    self._adapter_command_topic,
                    None,
                    envelope.model_dump_json(exclude_none=True).encode("utf-8"),
                    None,
                )
                return await asyncio.wait_for(
                    result_queue.get(),
                    timeout=command.timeout_seconds,
                )
            finally:
                await _call_unsubscribe(unsubscribe)
        finally:
            await self._bus.close()

    async def _on_adapter_command(
        self,
        message: object,
        result_queue: asyncio.Queue[
            tuple[ModelDispatchBusTerminalResult, ModelLocalRuntimeEvidence]
        ],
    ) -> None:
        command = _parse_adapter_command(_message_value(message))
        if command is None:
            return

        route = _resolve_node_route(command.command_name)
        events: list[dict[str, str]] = []
        node_terminal_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
        unsubscribe_terminal = await self._bus.subscribe(
            route.terminal_topic,
            None,
            lambda terminal_message: _capture_terminal(
                terminal_message, node_terminal_queue
            ),
            group_id=f"codex-local-terminal-{command.correlation_id.hex}",
        )
        unsubscribe_node = await self._bus.subscribe(
            route.command_topic,
            None,
            lambda node_message: self._on_node_command(
                node_message,
                route=route,
                events=events,
            ),
            group_id=f"codex-local-node-{route.node_name}-{command.correlation_id.hex}",
        )
        unsubscribe_delegation = await self._install_local_delegation_effect(events)

        try:
            await self._persist_invocation(command, route)
            input_model = _import_attr(route.input_model_module, route.input_model_name)
            payload_data = _payload_with_command_correlation_id(
                input_model,
                command.payload,
                command.correlation_id,
            )
            payload = input_model.model_validate(payload_data)
            node_envelope = ModelEventEnvelope[Any](
                payload=payload,
                correlation_id=command.correlation_id,
                envelope_timestamp=datetime.now(UTC),
                event_type=route.command_topic,
                source_tool=_LOCAL_RUNTIME_SOURCE,
            )
            await self._bus.publish(
                route.command_topic,
                None,
                node_envelope.model_dump_json(exclude_none=True).encode("utf-8"),
                None,
            )
            events.append(
                {
                    "topic": route.command_topic,
                    "event": "node_command_published",
                }
            )
            terminal_payload = await asyncio.wait_for(
                node_terminal_queue.get(),
                timeout=command.timeout_seconds,
            )
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=command.correlation_id,
                status=str(terminal_payload.pop("_runtime_status", "completed")),
                payload=terminal_payload,
                error_message=cast(str | None, terminal_payload.get("error_message")),
            )
        except Exception as exc:
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=command.correlation_id,
                status="failed",
                error_message=str(exc),
            )

        evidence = self._build_evidence(route, events)
        await result_queue.put((terminal, evidence))
        await _call_unsubscribe(unsubscribe_terminal)
        await _call_unsubscribe(unsubscribe_node)
        await _call_unsubscribe(unsubscribe_delegation)

    async def _on_node_command(
        self,
        message: object,
        *,
        route: _NodeRoute,
        events: list[dict[str, str]],
    ) -> None:
        try:
            payload_model = _import_attr(
                route.input_model_module, route.input_model_name
            )
            handler = _instantiate_handler(
                route.handler_module,
                route.handler_class,
                event_bus=self._bus,
                state_store=self._state_store,
            )
            payload = payload_model.model_validate(_extract_envelope_payload(message))
            result = await _invoke_handler(handler, payload)
            result_payload = _serialize_result(result)
            result_payload["_runtime_status"] = _status_for_result(result_payload)
            terminal_envelope = ModelEventEnvelope[dict[str, object]](
                payload=result_payload,
                correlation_id=_extract_correlation_id(message),
                envelope_timestamp=datetime.now(UTC),
                event_type=route.terminal_topic,
                source_tool=_LOCAL_RUNTIME_SOURCE,
            )
            await self._bus.publish(
                route.terminal_topic,
                None,
                terminal_envelope.model_dump_json(exclude_none=True).encode("utf-8"),
                None,
            )
            events.append(
                {
                    "topic": route.terminal_topic,
                    "event": "node_terminal_published",
                }
            )
        except Exception as exc:
            failure_payload: dict[str, object] = {
                "_runtime_status": "failed",
                "error_message": str(exc),
            }
            terminal_envelope = ModelEventEnvelope[dict[str, object]](
                payload=failure_payload,
                correlation_id=_extract_correlation_id(message),
                envelope_timestamp=datetime.now(UTC),
                event_type=route.failure_topic or route.terminal_topic,
                source_tool=_LOCAL_RUNTIME_SOURCE,
            )
            await self._bus.publish(
                route.failure_topic or route.terminal_topic,
                None,
                terminal_envelope.model_dump_json(exclude_none=True).encode("utf-8"),
                None,
            )

    async def _install_local_delegation_effect(
        self, events: list[dict[str, str]]
    ) -> Callable[[], Awaitable[None]]:
        async def on_delegation_request(message: object) -> None:
            request = ModelDelegationRequest.model_validate(
                _extract_envelope_payload(message)
            )
            result = ModelDelegationResult(
                correlation_id=request.correlation_id,
                task_type=request.task_type,
                model_used="local-runtime-fallback",
                endpoint_url="local://inmemory-delegation-effect",
                content="Delegation request completed by local runtime fallback.",
                quality_passed=True,
                quality_score=1.0,
                latency_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                fallback_to_claude=False,
                failure_reason="",
            )
            envelope = ModelEventEnvelope[ModelDelegationResult](
                payload=result,
                correlation_id=request.correlation_id,
                envelope_timestamp=datetime.now(UTC),
                event_type=_DELEGATION_COMPLETED_TOPIC,
                source_tool="local-delegation-effect",
            )
            await self._bus.publish(
                _DELEGATION_COMPLETED_TOPIC,
                None,
                envelope.model_dump_json(exclude_none=True).encode("utf-8"),
                None,
            )
            events.append(
                {
                    "topic": _DELEGATION_COMPLETED_TOPIC,
                    "event": "local_delegation_terminal_published",
                }
            )

        return await self._bus.subscribe(
            _DELEGATION_REQUEST_TOPIC,
            None,
            on_delegation_request,
            group_id="codex-local-delegation-effect",
        )

    async def _persist_invocation(
        self,
        command: ModelDispatchBusCommand,
        route: _NodeRoute,
    ) -> None:
        fingerprint = hashlib.sha256(route.contract_path.read_bytes()).hexdigest()
        envelope = ModelStateEnvelope(
            node_id=route.node_name,
            scope_id=str(command.correlation_id),
            data={
                "command_name": command.command_name,
                "command_topic": route.command_topic,
                "terminal_topic": route.terminal_topic,
                "runtime_mode": "local",
            },
            written_at=datetime.now(UTC),
            contract_fingerprint=fingerprint,
        )
        await self._state_store.put(envelope)

    def _build_evidence(
        self,
        route: _NodeRoute,
        events: list[dict[str, str]],
    ) -> ModelLocalRuntimeEvidence:
        return ModelLocalRuntimeEvidence(
            state_root=str(self._state_store._state_root),  # noqa: SLF001
            node_name=route.node_name,
            node_contract=str(route.contract_path),
            adapter_command_topic=self._adapter_command_topic,
            command_topic=route.command_topic,
            terminal_topic=route.terminal_topic,
            payload_model=route.payload_model_ref,
            handler_route=route.handler_ref,
            fallback_reason=self._fallback_reason,
            events=events,
        )


def _resolve_node_route(command_name: str) -> _NodeRoute:
    node_name = _node_name_for_command(command_name)
    contract_path = _NODE_ROOT / node_name / "contract.yaml"
    if not contract_path.exists():
        raise ValueError(f"No local node contract found for command {command_name!r}")
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(contract, dict):
        raise ValueError(f"Node contract must be a mapping: {contract_path}")

    event_bus = cast(dict[str, Any], contract.get("event_bus") or {})
    runtime_dispatch = cast(dict[str, Any], contract.get("runtime_dispatch") or {})
    terminals = cast(
        dict[str, Any],
        runtime_dispatch.get("terminal_events")
        or contract.get("terminal_events")
        or {},
    )
    input_model = _input_model_spec(contract, node_name=node_name)
    handler = _handler_spec(contract)

    command_topic = str(
        runtime_dispatch.get("command_topic")
        or _first_string(event_bus.get("subscribe_topics"))
        or ""
    )
    terminal_topic = str(
        contract.get("terminal_event")
        or terminals.get("success")
        or _completed_topic(event_bus.get("publish_topics"))
        or ""
    )
    if not command_topic or not terminal_topic:
        raise ValueError(f"Contract lacks command/terminal topics: {contract_path}")

    return _NodeRoute(
        node_name=node_name,
        contract_path=contract_path,
        command_topic=command_topic,
        terminal_topic=terminal_topic,
        failure_topic=cast(str | None, terminals.get("failure")),
        input_model_module=input_model[0],
        input_model_name=input_model[1],
        handler_module=handler[0],
        handler_class=handler[1],
    )


def _node_name_for_command(command_name: str) -> str:
    if command_name in {"delegate_skill", "delegate_skill.orchestrate"}:
        return "node_delegate_skill_orchestrator"
    candidate = command_name.split(".", 1)[0].replace("-", "_")
    if candidate.startswith("node_"):
        return candidate
    return f"node_{candidate}"


def _input_model_spec(contract: dict[str, Any], *, node_name: str) -> tuple[str, str]:
    handler = cast(dict[str, Any], contract.get("handler") or {})
    raw = handler.get("input_model") or contract.get("input_model") or {}
    if isinstance(raw, str) and "." in raw:
        module, name = raw.rsplit(".", 1)
        return module, name
    if isinstance(raw, str):
        resolved = _model_ref_from_local_name(node_name, raw)
        if resolved is not None:
            return resolved
    if isinstance(raw, dict):
        resolved = _model_ref_from_mapping(raw, node_name=node_name)
        if resolved is not None:
            return resolved
    models = contract.get("models")
    if isinstance(models, dict):
        model_input = models.get("input")
        if isinstance(model_input, dict):
            resolved = _model_ref_from_mapping(model_input, node_name=node_name)
            if resolved is not None:
                return resolved
    route_model = _first_handler_model_spec(contract, node_name=node_name)
    if route_model is not None:
        return route_model
    raise ValueError("Contract lacks an input_model module/name")


def _handler_spec(contract: dict[str, Any]) -> tuple[str, str]:
    handler = cast(dict[str, Any], contract.get("handler") or {})
    module = str(handler.get("module") or "")
    name = str(handler.get("class") or handler.get("name") or "")
    if module and name:
        return module, name
    routing = cast(dict[str, Any], contract.get("handler_routing") or {})
    handlers = routing.get("handlers")
    if isinstance(handlers, list) and handlers:
        first = cast(dict[str, Any], handlers[0])
        module = str(first.get("handler_module") or "")
        name = str(first.get("handler_class") or "")
        if module and name:
            return module, name
        nested_handler = first.get("handler")
        if isinstance(nested_handler, dict):
            module = str(nested_handler.get("module") or "")
            name = str(
                nested_handler.get("class")
                or nested_handler.get("name")
                or nested_handler.get("handler_class")
                or ""
            )
            if module and name:
                return module, name
    raise ValueError("Contract lacks a handler module/class")


def _first_handler_model_spec(
    contract: dict[str, Any], *, node_name: str
) -> tuple[str, str] | None:
    routing = cast(dict[str, Any], contract.get("handler_routing") or {})
    handlers = routing.get("handlers")
    if not isinstance(handlers, list):
        return None
    for raw_handler in handlers:
        if not isinstance(raw_handler, dict):
            continue
        for key in ("input_model", "event_model"):
            raw_model = raw_handler.get(key)
            if isinstance(raw_model, str) and "." in raw_model:
                module, name = raw_model.rsplit(".", 1)
                return module, name
            if isinstance(raw_model, str):
                resolved = _model_ref_from_local_name(node_name, raw_model)
                if resolved is not None:
                    return resolved
            if isinstance(raw_model, dict):
                resolved = _model_ref_from_mapping(raw_model, node_name=node_name)
                if resolved is not None:
                    return resolved
    return None


def _model_ref_from_mapping(
    raw: dict[str, Any], *, node_name: str
) -> tuple[str, str] | None:
    module = str(raw.get("module") or "")
    name = str(raw.get("class") or raw.get("name") or "")
    if not module or not name:
        return None
    if module.endswith(f".{name}"):
        module, name = module.rsplit(".", 1)
    elif "." not in module:
        resolved = _model_ref_from_local_name(node_name, name)
        if resolved is not None:
            return resolved
    return module, name


def _model_ref_from_local_name(
    node_name: str, model_name: str
) -> tuple[str, str] | None:
    if "." in model_name:
        module, name = model_name.rsplit(".", 1)
        return module, name
    node_dir = _NODE_ROOT / node_name / "models"
    if not node_dir.is_dir():
        return None
    class_pattern = f"class {model_name}"
    for model_file in sorted(node_dir.glob("*.py")):
        if model_file.name == "__init__.py":
            continue
        try:
            source = model_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if class_pattern in source:
            module = f"omnimarket.nodes.{node_name}.models.{model_file.stem}"
            return module, model_name

    init_file = node_dir / "__init__.py"
    try:
        init_source = init_file.read_text(encoding="utf-8")
    except OSError:
        return None
    if model_name in init_source:
        return f"omnimarket.nodes.{node_name}.models", model_name
    return None


def _first_string(value: object) -> str | None:
    if isinstance(value, list):
        return next(
            (str(item) for item in value if isinstance(item, str) and item), None
        )
    return None


def _completed_topic(value: object) -> str | None:
    if isinstance(value, list):
        return next(
            (
                str(item)
                for item in value
                if isinstance(item, str) and "completed" in item
            ),
            _first_string(value),
        )
    return None


def _import_attr(module_name: str, attr_name: str) -> Any:
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def _payload_with_command_correlation_id(
    input_model: Any,
    payload: dict[str, object],
    correlation_id: UUID,
) -> dict[str, object]:
    model_fields = getattr(input_model, "model_fields", {})
    if not isinstance(model_fields, dict) or "correlation_id" not in model_fields:
        return payload
    stamped = dict(payload)
    stamped["correlation_id"] = str(correlation_id)
    return stamped


def _instantiate_handler(
    module_name: str,
    class_name: str,
    *,
    event_bus: EventBusInmemory,
    state_store: _LocalRuntimeStateStore,
) -> Any:
    cls = _import_attr(module_name, class_name)
    kwargs: dict[str, object] = {}
    try:
        signature = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        if "event_bus" in signature.parameters:
            kwargs["event_bus"] = event_bus
        if "state_store" in signature.parameters:
            kwargs["state_store"] = state_store
    return cls(**kwargs)


async def _invoke_handler(handler: Any, payload: object) -> Any:
    method = getattr(handler, "handle", None)
    if method is None:
        method = getattr(handler, "run", None) or getattr(handler, "execute", None)
    if method is None:
        raise TypeError(f"{type(handler).__name__} has no handle/run/execute method")
    result = method(payload)
    if inspect.isawaitable(result):
        return await result
    return result


def _serialize_result(result: object) -> dict[str, object]:
    if result is None:
        return {"status": "completed"}
    if hasattr(result, "model_dump"):
        return cast(
            dict[str, object],
            result.model_dump(mode="json", exclude_none=True),
        )
    if isinstance(result, dict):
        return cast(dict[str, object], result)
    return {"result": json.loads(json.dumps(result, default=repr))}


def _status_for_result(payload: dict[str, object]) -> str:
    if str(payload.get("status", "completed")) in {"failed", "failure", "timeout"}:
        return str(payload["status"])
    return "completed"


def _message_value(message: object) -> bytes | str | None:
    value = getattr(message, "value", None)
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, bytes | str):
        return value
    return None


def _parse_adapter_command(value: bytes | str | None) -> ModelDispatchBusCommand | None:
    if value is None:
        return None
    try:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            value
        )
    except Exception:
        return None
    return envelope.payload


def _extract_envelope_payload(message: object) -> dict[str, object]:
    value = _message_value(message)
    if value is None:
        raise ValueError("message has no bytes/string value")
    raw = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
    payload = raw.get("payload", raw)
    if not isinstance(payload, dict):
        raise ValueError("event envelope payload must be an object")
    return cast(dict[str, object], payload)


def _extract_correlation_id(message: object) -> UUID:
    value = _message_value(message)
    if value is None:
        raise ValueError("message has no bytes/string value")
    raw = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
    return UUID(str(raw.get("correlation_id")))


async def _capture_terminal(
    message: object,
    queue: asyncio.Queue[dict[str, object]],
) -> None:
    payload = _extract_envelope_payload(message)
    if queue.empty():
        await queue.put(payload)


async def _call_unsubscribe(unsubscribe: object) -> None:
    if callable(unsubscribe):
        result = unsubscribe()
        if inspect.isawaitable(result):
            await result


__all__ = [
    "LocalRuntimeDispatch",
    "ModelLocalRuntimeEvidence",
]
