# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event-bus backed delegation dispatch port."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusCommand,
    ModelDispatchBusTerminalResult,
    default_command_topic,
    default_response_topic,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)

_INTERNAL_COMMAND_NAME = "node_delegation_orchestrator"
_REQUESTER = "delegate-skill-runtime-port"


class ProtocolDelegationEventBus(Protocol):
    """Event bus surface required by the runtime delegation dispatch port."""

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        node_identity: object | None = None,
        on_message: Callable[[object], Awaitable[None]] | None = None,
        **kwargs: object,
    ) -> object: ...


class RuntimeDelegationDispatchPort:
    """Dispatch consumer-facing delegation requests into the runtime bus."""

    def __init__(
        self,
        *,
        event_bus: ProtocolDelegationEventBus,
        command_topic: str | None = None,
        response_topic: str | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._command_topic = command_topic or default_command_topic()
        self._response_topic = response_topic or default_response_topic()

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: UUID,
        max_tokens: int,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
    ) -> dict[str, object]:
        dispatch_correlation_id = uuid4()
        request = ModelDelegationRequest(
            prompt=prompt,
            task_type=cast("Any", task_type),
            source_session_id=source_session_id,
            source_file_path=source_file_path,
            correlation_id=dispatch_correlation_id,
            max_tokens=max_tokens,
            emitted_at=datetime.now(UTC),
        )
        command = ModelDispatchBusCommand(
            command_name=_INTERNAL_COMMAND_NAME,
            requester=_REQUESTER,
            payload=request.model_dump(mode="json"),
            correlation_id=dispatch_correlation_id,
            response_topic=self._response_topic,
            timeout_seconds=300.0,
        )

        if not wait:
            await self._publish_command(command)
            return {
                "status": "completed",
                "content": "",
                "delegated_to": "runtime",
                "model_name": "",
                "quality_gate_passed": False,
            }

        unsubscribe, queue = await self._subscribe_for_result(dispatch_correlation_id)
        try:
            await self._publish_command(command)
            terminal = await asyncio.wait_for(
                queue.get(), timeout=command.timeout_seconds
            )
        except TimeoutError:
            return {
                "status": "timeout",
                "error_message": (
                    f"timed out after {command.timeout_seconds:g}s waiting for "
                    "delegation result"
                ),
            }
        finally:
            await _unsubscribe(unsubscribe)

        result: dict[str, object] = {"status": terminal.status}
        if terminal.error_message:
            result["error_message"] = terminal.error_message
        if terminal.payload:
            result.update(_flatten_terminal_payload(terminal.payload))
        return result

    async def _publish_command(self, command: ModelDispatchBusCommand) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand](
            payload=command,
            correlation_id=command.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=self._command_topic,
            source_tool=_REQUESTER,
        )
        await self._event_bus.publish(
            self._command_topic,
            None,
            envelope.model_dump_json(exclude_none=True).encode("utf-8"),
            None,
        )

    async def _subscribe_for_result(
        self, dispatch_correlation_id: UUID
    ) -> tuple[object, asyncio.Queue[ModelDispatchBusTerminalResult]]:
        queue: asyncio.Queue[ModelDispatchBusTerminalResult] = asyncio.Queue()

        async def on_message(message: object) -> None:
            value = _message_value(message)
            if value is None:
                return
            try:
                envelope = ModelEventEnvelope[
                    ModelDispatchBusTerminalResult
                ].model_validate_json(value)
            except ValueError:
                return
            if envelope.payload.correlation_id != dispatch_correlation_id:
                return
            await queue.put(envelope.payload)

        unsubscribe = await self._event_bus.subscribe(
            self._response_topic,
            None,
            on_message,
            group_id=f"delegate-skill-runtime-port-{dispatch_correlation_id.hex}",
        )
        return unsubscribe, queue


def _message_value(message: object) -> bytes | str | None:
    raw = getattr(message, "value", None)
    if isinstance(raw, bytearray):
        return bytes(raw)
    if isinstance(raw, bytes | str):
        return raw
    return None


def _flatten_terminal_payload(payload: dict[str, object]) -> dict[str, object]:
    nested_payload = payload.get("payload")
    if isinstance(nested_payload, dict):
        flattened = dict(nested_payload)
        topic = payload.get("topic")
        if isinstance(topic, str) and topic:
            flattened["terminal_topic"] = topic
        return flattened
    return payload


async def _unsubscribe(unsubscribe: object) -> None:
    if not callable(unsubscribe):
        return
    result = unsubscribe()
    if asyncio.iscoroutine(result):
        await result
