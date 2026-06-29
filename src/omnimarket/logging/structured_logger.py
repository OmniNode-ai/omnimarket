"""StructuredEventLogger — drop-in logger that emits ModelStructuredLogEntry events.

Any ONEX handler can instantiate this logger to publish structured log entries
as onex.evt.platform.log-entry.v1 events, which node_log_projection consumes.

Schema aligned to ModelStructuredLogEntry (omnibase_core) per OMN-13703.
"""

from __future__ import annotations

import json
from typing import Protocol
from uuid import UUID, uuid4

from omnibase_core.enums.enum_log_entry_status import EnumLogEntryStatus
from omnibase_core.enums.enum_log_level import EnumLogLevel
from omnibase_core.enums.enum_redaction_state import EnumRedactionState
from omnibase_core.enums.enum_suppression_decision import EnumSuppressionDecision
from omnibase_core.models.logging.model_structured_log_entry import (
    ModelStructuredLogEntry,
)

from omnimarket.logging.topics import LOG_ENTRY_TOPIC

# Re-export LOG_ENTRY_TOPIC so existing callers that import it from here still work.
__all__ = ["LOG_ENTRY_TOPIC", "StructuredEventLogger"]


class EventBusProtocol(Protocol):
    """Minimal event bus interface for publishing."""

    async def publish(self, topic: str, *, key: bytes | None, value: bytes) -> None: ...


def _coerce_uuid(value: str | UUID | None) -> UUID | None:
    """Convert a string correlation/session/node id to UUID, or return None.

    Callers may pass raw UUID strings for convenience.
    Non-UUID strings silently become None — they cannot round-trip through the
    typed ModelStructuredLogEntry.correlation_id field.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except ValueError:
        return None


class StructuredEventLogger:
    """Drop-in logger that emits ModelStructuredLogEntry events to the event bus.

    Emitted payloads are always schema-valid against
    ``omnibase_core.models.logging.model_structured_log_entry.ModelStructuredLogEntry``.

    Usage:
        logger = StructuredEventLogger("node_build_loop", event_bus=bus)
        await logger.info("Phase transition complete", operation="advance")
    """

    def __init__(
        self, source_system: str, event_bus: EventBusProtocol | None = None
    ) -> None:
        self._source_system = source_system
        self._event_bus = event_bus

    def _build_entry(
        self,
        level: EnumLogLevel,
        message: str,
        *,
        operation: str = "",
        correlation_id: str | UUID | None = None,
        duration_ms: float | None = None,
        node_id: str | UUID | None = None,
        session_id: str | UUID | None = None,
        artifact_refs: list[str] | None = None,
        extra_metadata: dict[str, str] | None = None,
    ) -> ModelStructuredLogEntry:
        """Build a schema-valid ModelStructuredLogEntry.

        Parameters
        ----------
        level:
            Severity level for this entry.
        message:
            Human-readable log message.
        operation:
            The function or operation within ``source_system`` that emitted
            this entry.
        correlation_id:
            Cross-boundary correlation ID; str is accepted for convenience and
            coerced to UUID.  Non-UUID strings become None.
        duration_ms:
            Execution duration; serialised into ``metadata["duration_ms"]``
            when provided.
        node_id:
            ONEX node UUID; coerced from str where possible.
        session_id:
            Session UUID; coerced from str where possible.
        artifact_refs:
            List of opaque artifact ID strings.
        extra_metadata:
            Additional string key-value pairs appended to ``metadata``.
        """
        meta: dict[str, str] = dict(extra_metadata or {})
        if duration_ms is not None:
            meta["duration_ms"] = str(duration_ms)

        return ModelStructuredLogEntry(
            entry_id=uuid4(),
            source_system=self._source_system,
            operation=operation,
            level=level,
            message=message,
            status=EnumLogEntryStatus.EMITTED,
            redaction_state=EnumRedactionState.NONE,
            suppression_decision=EnumSuppressionDecision.EMIT,
            correlation_id=_coerce_uuid(correlation_id),
            node_id=_coerce_uuid(node_id),
            session_id=_coerce_uuid(session_id),
            artifact_refs=artifact_refs or [],
            metadata=meta,
        )

    async def _emit(self, entry: ModelStructuredLogEntry) -> ModelStructuredLogEntry:
        if self._event_bus is not None:
            payload = json.dumps(entry.model_dump(mode="json")).encode()
            await self._event_bus.publish(LOG_ENTRY_TOPIC, key=None, value=payload)
        return entry

    async def debug(
        self,
        message: str,
        *,
        operation: str = "",
        correlation_id: str | UUID | None = None,
        duration_ms: float | None = None,
        **metadata: str,
    ) -> ModelStructuredLogEntry:
        entry = self._build_entry(
            EnumLogLevel.DEBUG,
            message,
            operation=operation,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            extra_metadata=metadata,
        )
        return await self._emit(entry)

    async def info(
        self,
        message: str,
        *,
        operation: str = "",
        correlation_id: str | UUID | None = None,
        duration_ms: float | None = None,
        **metadata: str,
    ) -> ModelStructuredLogEntry:
        entry = self._build_entry(
            EnumLogLevel.INFO,
            message,
            operation=operation,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            extra_metadata=metadata,
        )
        return await self._emit(entry)

    async def warning(
        self,
        message: str,
        *,
        operation: str = "",
        correlation_id: str | UUID | None = None,
        duration_ms: float | None = None,
        **metadata: str,
    ) -> ModelStructuredLogEntry:
        entry = self._build_entry(
            EnumLogLevel.WARNING,
            message,
            operation=operation,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            extra_metadata=metadata,
        )
        return await self._emit(entry)

    async def error(
        self,
        message: str,
        *,
        operation: str = "",
        correlation_id: str | UUID | None = None,
        duration_ms: float | None = None,
        **metadata: str,
    ) -> ModelStructuredLogEntry:
        entry = self._build_entry(
            EnumLogLevel.ERROR,
            message,
            operation=operation,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            extra_metadata=metadata,
        )
        return await self._emit(entry)

    async def critical(
        self,
        message: str,
        *,
        operation: str = "",
        correlation_id: str | UUID | None = None,
        duration_ms: float | None = None,
        **metadata: str,
    ) -> ModelStructuredLogEntry:
        entry = self._build_entry(
            EnumLogLevel.CRITICAL,
            message,
            operation=operation,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            extra_metadata=metadata,
        )
        return await self._emit(entry)
