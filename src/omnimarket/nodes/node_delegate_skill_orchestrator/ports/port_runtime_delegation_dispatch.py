# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event-bus backed delegation dispatch port."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.topic_constants import (
    TOPIC_DELEGATION_COMPLETED,
    TOPIC_DELEGATION_FAILED,
    TOPIC_DELEGATION_REQUEST,
)

from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusTerminalResult,
)
from omnimarket.events.delegation import ModelDelegationRequest

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
    ) -> Callable[[], Awaitable[None]]: ...


class RuntimeDelegationDispatchPort:
    """Dispatch consumer-facing delegation requests into the runtime bus."""

    def __init__(
        self,
        *,
        event_bus: ProtocolDelegationEventBus,
        command_topic: str | None = None,
        completed_topic: str | None = None,
        failed_topic: str | None = None,
        response_topic: str | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._command_topic = command_topic or TOPIC_DELEGATION_REQUEST
        self._completed_topic = (
            completed_topic or response_topic or TOPIC_DELEGATION_COMPLETED
        )
        self._failed_topic = failed_topic or TOPIC_DELEGATION_FAILED

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
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
    ) -> dict[str, object]:
        request = ModelDelegationRequest(
            prompt=prompt,
            task_type=cast("Any", task_type),
            source_session_id=source_session_id,
            source_file_path=source_file_path,
            correlation_id=correlation_id,
            max_tokens=max_tokens,
            emitted_at=datetime.now(UTC),
            quality_contract_mode=cast("Any", quality_contract_mode),
            acceptance_criteria=acceptance_criteria,
        )

        if not wait:
            await self._publish_request(request)
            return {
                "status": "completed",
                "content": "",
                "delegated_to": "runtime",
                "model_name": "",
                "quality_gate_passed": False,
            }

        unsubscribe, queue = await self._subscribe_for_result(correlation_id)
        try:
            await self._publish_request(request)
            terminal = await asyncio.wait_for(queue.get(), timeout=300.0)
        except TimeoutError:
            return {
                "status": "timeout",
                "error_message": "timed out after 300s waiting for delegation result",
            }
        finally:
            await _unsubscribe(unsubscribe)

        result: dict[str, object] = {
            "status": terminal.status,
            "correlation_id": str(correlation_id),
        }
        if terminal.error_message:
            result["error_message"] = terminal.error_message
        if terminal.payload:
            result.update(_flatten_terminal_payload(terminal.payload))
        return result

    async def _publish_request(self, request: ModelDelegationRequest) -> None:
        envelope = ModelEventEnvelope[ModelDelegationRequest](
            payload=request,
            correlation_id=request.correlation_id,
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
    ) -> tuple[
        Callable[[], Awaitable[None]], asyncio.Queue[ModelDispatchBusTerminalResult]
    ]:
        queue: asyncio.Queue[ModelDispatchBusTerminalResult] = asyncio.Queue()

        async def on_message(message: object) -> None:
            value = _message_value(message)
            if value is None:
                return
            terminal = _parse_delegation_terminal(
                value,
                expected_correlation_id=dispatch_correlation_id,
            )
            if terminal is None:
                return
            await queue.put(terminal)

        unsubscribe_completed = await self._event_bus.subscribe(
            self._completed_topic,
            None,
            on_message,
            group_id=f"delegate-skill-runtime-port-{dispatch_correlation_id.hex}",
        )
        unsubscribe_failed = await self._event_bus.subscribe(
            self._failed_topic,
            None,
            on_message,
            group_id=f"delegate-skill-runtime-port-{dispatch_correlation_id.hex}",
        )

        async def unsubscribe() -> None:
            await unsubscribe_completed()
            await unsubscribe_failed()

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


def _parse_delegation_terminal(
    value: bytes | str,
    *,
    expected_correlation_id: UUID,
) -> ModelDispatchBusTerminalResult | None:
    try:
        raw = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    envelope_payload = raw.get("payload", raw)
    if not isinstance(envelope_payload, dict):
        return None

    terminal_payload = _flatten_terminal_payload(
        cast(dict[str, object], envelope_payload)
    )
    raw_correlation_id = terminal_payload.get("correlation_id")
    try:
        correlation_id = UUID(str(raw_correlation_id))
    except (TypeError, ValueError):
        return None
    if correlation_id != expected_correlation_id:
        return None

    topic = str(envelope_payload.get("topic") or raw.get("event_type") or "")
    is_failed = topic == TOPIC_DELEGATION_FAILED or bool(
        terminal_payload.get("failure_reason")
    )
    error_message = str(terminal_payload.get("failure_reason") or "") or None
    return ModelDispatchBusTerminalResult(
        correlation_id=correlation_id,
        status="failed" if is_failed else "completed",
        payload=cast(dict[str, object], envelope_payload),
        error_message=error_message,
    )


async def _unsubscribe(unsubscribe: Callable[[], Awaitable[None]]) -> None:
    await unsubscribe()
