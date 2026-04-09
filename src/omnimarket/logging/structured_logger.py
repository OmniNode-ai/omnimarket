"""StructuredEventLogger — drop-in logger that emits log events to the event bus.

Any ONEX handler can instantiate this logger to publish structured log entries
as onex.evt.platform.log-entry.v1 events, which node_log_projection consumes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from omnimarket.logging.topics import LOG_ENTRY_TOPIC
from omnimarket.nodes.node_log_projection.handlers.handler_log_projection import (
    EnumLogLevel,
    ModelLogEntry,
)


class EventBusProtocol(Protocol):
    """Minimal event bus interface for publishing."""

    async def publish(self, topic: str, *, key: bytes | None, value: bytes) -> None: ...


class StructuredEventLogger:
    """Drop-in logger that emits log events to the event bus.

    Usage:
        logger = StructuredEventLogger("node_build_loop", event_bus=bus)
        await logger.info("Phase transition complete", function_name="advance")
    """

    def __init__(
        self, node_name: str, event_bus: EventBusProtocol | None = None
    ) -> None:
        self._node_name = node_name
        self._event_bus = event_bus

    def _build_entry(
        self,
        level: EnumLogLevel,
        message: str,
        *,
        function_name: str = "",
        correlation_id: str | None = None,
        duration_ms: float | None = None,
        **metadata: str,
    ) -> ModelLogEntry:
        return ModelLogEntry(
            entry_id=str(uuid4()),
            timestamp=datetime.now(tz=UTC).isoformat(),
            node_name=self._node_name,
            function_name=function_name,
            level=level,
            message=message,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            metadata=metadata,
        )

    async def _emit(self, entry: ModelLogEntry) -> ModelLogEntry:
        if self._event_bus is not None:
            payload = json.dumps(entry.model_dump(mode="json")).encode()
            await self._event_bus.publish(LOG_ENTRY_TOPIC, key=None, value=payload)
        return entry

    async def debug(
        self,
        message: str,
        *,
        function_name: str = "",
        correlation_id: str | None = None,
        duration_ms: float | None = None,
        **metadata: str,
    ) -> ModelLogEntry:
        entry = self._build_entry(
            EnumLogLevel.DEBUG,
            message,
            function_name=function_name,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            **metadata,
        )
        return await self._emit(entry)

    async def info(
        self,
        message: str,
        *,
        function_name: str = "",
        correlation_id: str | None = None,
        duration_ms: float | None = None,
        **metadata: str,
    ) -> ModelLogEntry:
        entry = self._build_entry(
            EnumLogLevel.INFO,
            message,
            function_name=function_name,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            **metadata,
        )
        return await self._emit(entry)

    async def warning(
        self,
        message: str,
        *,
        function_name: str = "",
        correlation_id: str | None = None,
        duration_ms: float | None = None,
        **metadata: str,
    ) -> ModelLogEntry:
        entry = self._build_entry(
            EnumLogLevel.WARNING,
            message,
            function_name=function_name,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            **metadata,
        )
        return await self._emit(entry)

    async def error(
        self,
        message: str,
        *,
        function_name: str = "",
        correlation_id: str | None = None,
        duration_ms: float | None = None,
        **metadata: str,
    ) -> ModelLogEntry:
        entry = self._build_entry(
            EnumLogLevel.ERROR,
            message,
            function_name=function_name,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            **metadata,
        )
        return await self._emit(entry)

    async def critical(
        self,
        message: str,
        *,
        function_name: str = "",
        correlation_id: str | None = None,
        duration_ms: float | None = None,
        **metadata: str,
    ) -> ModelLogEntry:
        entry = self._build_entry(
            EnumLogLevel.CRITICAL,
            message,
            function_name=function_name,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            **metadata,
        )
        return await self._emit(entry)
