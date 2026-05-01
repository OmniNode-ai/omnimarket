"""Terminal-event wait adapter for Pattern B broker dispatch requests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any, Protocol

from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_contract_config import (
    load_pattern_b_broker_config,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerEventType,
    EnumPatternBBrokerState,
    EnumPatternBBrokerTerminalStatus,
    ModelPatternBBrokerDispatchRequest,
    ModelPatternBBrokerRuntimeConfig,
    ModelPatternBBrokerTerminalEvent,
)


class ProtocolPatternBBrokerEventMessage(Protocol):
    """Minimal event message shape consumed by the terminal wait adapter."""

    value: bytes | str | Mapping[str, Any]


class ProtocolPatternBBrokerEventSubscriber(Protocol):
    """Minimal subscribe surface required by the terminal wait adapter."""

    async def subscribe(
        self,
        topic: str,
        node_identity: Any | None = None,
        on_message: Callable[[Any], Awaitable[None]] | None = None,
        *,
        group_id: str | None = None,
    ) -> Callable[[], Awaitable[None]]: ...


class AdapterPatternBBrokerTerminalConsumer:
    """Wait for typed terminal events that match a broker dispatch request."""

    def __init__(
        self,
        *,
        event_bus: ProtocolPatternBBrokerEventSubscriber,
        config: ModelPatternBBrokerRuntimeConfig | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._config = config or load_pattern_b_broker_config()

    @property
    def config(self) -> ModelPatternBBrokerRuntimeConfig:
        return self._config

    async def wait_for_terminal_event(
        self,
        request: ModelPatternBBrokerDispatchRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> ModelPatternBBrokerTerminalEvent:
        """Return the first correlated terminal event, or a typed timeout event."""
        timeout = timeout_seconds
        if timeout is None:
            timeout = float(request.wait_policy.timeout_seconds)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ModelPatternBBrokerTerminalEvent] = loop.create_future()

        async def on_message(message: ProtocolPatternBBrokerEventMessage) -> None:
            if future.done():
                return
            event = _parse_terminal_event(message)
            if event is None:
                return
            if event.request_id != request.request_id:
                return
            if event.correlation_id != request.correlation_id:
                return
            future.set_result(event)

        group_id = f"{self._config.consumer_group}-{request.request_id.hex[:8]}"
        unsubscribers: list[Callable[[], Awaitable[None]]] = []

        try:
            unsubscribers.append(
                await self._event_bus.subscribe(
                    self._config.topics.terminal_completed_topic,
                    on_message=on_message,
                    group_id=group_id,
                )
            )
            unsubscribers.append(
                await self._event_bus.subscribe(
                    self._config.topics.terminal_failed_topic,
                    on_message=on_message,
                    group_id=group_id,
                )
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return ModelPatternBBrokerTerminalEvent(
                request_id=request.request_id,
                correlation_id=request.correlation_id,
                event_type=EnumPatternBBrokerEventType.terminal_timed_out,
                state=EnumPatternBBrokerState.timed_out,
                status=EnumPatternBBrokerTerminalStatus.timed_out,
                error_message=f"timed out after {timeout:g}s waiting for terminal event",
            )
        finally:
            await _cleanup_unsubscribers(unsubscribers)


def _parse_terminal_event(
    message: ProtocolPatternBBrokerEventMessage,
) -> ModelPatternBBrokerTerminalEvent | None:
    raw = message.value
    try:
        if isinstance(raw, bytes | bytearray):
            payload = json.loads(raw.decode("utf-8"))
        elif isinstance(raw, str):
            payload = json.loads(raw)
        elif isinstance(raw, Mapping):
            payload = dict(raw)
        else:
            return None
        return ModelPatternBBrokerTerminalEvent.model_validate(payload)
    except (TypeError, ValueError):
        return None


async def _cleanup_unsubscribers(
    unsubscribers: list[Callable[[], Awaitable[None]]],
) -> None:
    for unsubscribe in unsubscribers:
        with suppress(Exception):
            await unsubscribe()


__all__ = [
    "AdapterPatternBBrokerTerminalConsumer",
    "ProtocolPatternBBrokerEventMessage",
    "ProtocolPatternBBrokerEventSubscriber",
]
