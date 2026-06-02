# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tiered Event Queue for the emit daemon (durability split by topic tier).

Each event carries a declared per-topic durability tier and is routed by it:

    DUTY_CRITICAL -> append-only durable outbox. Acked replay outbox -> Kafka,
        truncate-on-ack, NEVER drop. On outbox-storage-full the enqueue raises
        ``DurableOutboxFullError`` (explicit backpressure, no silent drop).
    TELEMETRY -> bounded in-memory queue + disk spool with drop-oldest on
        overflow (loss correct by design for high-volume metrics).

Telemetry queue behavior:
    1. Events are first added to the in-memory queue
    2. When memory queue is full, events overflow to disk spool
    3. When disk spool is full (by message count or bytes), oldest events are dropped
    4. Dequeue prioritizes the durable outbox, then memory queue, then disk spool

Disk Spool / Outbox Format:
    - Files: {timestamp}_{event_id}.json (one event per file)
    - Sorted by filename for FIFO ordering

Acknowledgement: callers MUST call ``ack(event)`` after a confirmed Kafka
publish. For duty-critical events ``ack`` truncates the outbox record; for
telemetry events it is a no-op (the spool/memory entry is removed on dequeue).

Concurrency: coroutine-safe using asyncio.Lock (not thread-safe).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_emit_daemon.models.model_durability import (
    EnumDurabilityTier,
)
from omnimarket.nodes.node_emit_daemon.models.model_protocol import JsonType

logger = logging.getLogger(__name__)


class ModelQueuedEvent(BaseModel):
    """An event waiting to be published."""

    model_config = ConfigDict(
        strict=False,
        frozen=True,
        extra="forbid",
        from_attributes=True,
    )

    event_id: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    payload: JsonType = Field(...)
    partition_key: str | None = Field(default=None)
    queued_at: datetime = Field(...)
    tier: EnumDurabilityTier = Field(
        default=EnumDurabilityTier.TELEMETRY,
        description=(
            "Per-topic durability tier driving routing: DUTY_CRITICAL -> "
            "durable outbox (never drop); TELEMETRY -> bounded spool (drop "
            "oldest on overflow)."
        ),
    )

    @field_validator("queued_at", mode="before")
    @classmethod
    def ensure_utc_aware(cls, v: object) -> object:
        if not isinstance(v, datetime):
            return v
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        if v.utcoffset() == timedelta(0):
            if v.tzinfo is not UTC:
                return v.replace(tzinfo=UTC)
            return v
        return v.astimezone(UTC)


def _default_spool_dir() -> Path:
    """Default spool directory using XDG_RUNTIME_DIR or /tmp fallback."""
    import os

    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "onex" / "event-spool"
    return Path("/tmp") / "onex-event-spool"


def _default_outbox_dir() -> Path:
    """Default durable-outbox directory using XDG_RUNTIME_DIR or /tmp fallback."""
    import os

    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "onex" / "event-outbox"
    return Path("/tmp") / "onex-event-outbox"


class BoundedEventQueue:
    """Tiered queue: durable outbox for duty-critical, bounded spool for telemetry.

    Telemetry events use the bounded in-memory queue + disk spool with
    drop-oldest overflow. Duty-critical events are routed to an append-only
    durable outbox that never drops; when the outbox is full, ``enqueue``
    raises ``DurableOutboxFullError`` so the caller can surface backpressure.
    """

    def __init__(
        self,
        max_memory_queue: int = 100,
        max_spool_messages: int = 1000,
        max_spool_bytes: int = 10_485_760,  # 10 MB
        spool_dir: Path | None = None,
        outbox_dir: Path | None = None,
        max_outbox_messages: int = 100_000,
        max_outbox_bytes: int = 268_435_456,  # 256 MB
    ) -> None:
        self._max_memory_queue = max_memory_queue
        self._max_spool_messages = max_spool_messages
        self._max_spool_bytes = max_spool_bytes
        self._spool_dir = spool_dir if spool_dir is not None else _default_spool_dir()

        self._memory_queue: deque[ModelQueuedEvent] = deque()
        self._spool_files: deque[Path] = deque()
        self._spool_bytes: int = 0
        self._lock = asyncio.Lock()

        self._ensure_spool_dir()

        # Durable outbox for duty-critical events. Imported lazily to avoid a
        # module import cycle (durable_outbox imports ModelQueuedEvent here).
        from omnimarket.nodes.node_emit_daemon.durable_outbox import DurableOutbox

        resolved_outbox = (
            outbox_dir if outbox_dir is not None else _default_outbox_dir()
        )
        self._outbox = DurableOutbox(
            outbox_dir=resolved_outbox,
            max_messages=max_outbox_messages,
            max_bytes=max_outbox_bytes,
        )
        # Maps acked telemetry/duty event_ids to their outbox record for ack().
        self._in_flight_outbox: dict[str, object] = {}

    def _ensure_spool_dir(self) -> None:
        try:
            self._spool_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(
                f"Failed to create spool directory {self._spool_dir}: {e}. "
                "Disk spool will be unavailable."
            )

    async def enqueue(self, event: ModelQueuedEvent) -> bool:
        """Route an event by its durability tier.

        Duty-critical events go to the append-only durable outbox (never drop;
        raises ``DurableOutboxFullError`` on overflow). Telemetry events use
        the bounded memory/spool path with drop-oldest overflow.
        """
        if event.tier is EnumDurabilityTier.DUTY_CRITICAL:
            await self._outbox.append(event)
            return True

        async with self._lock:
            if len(self._memory_queue) < self._max_memory_queue:
                self._memory_queue.append(event)
                logger.debug(
                    f"Event {event.event_id} queued in memory "
                    f"(memory: {len(self._memory_queue)}/{self._max_memory_queue})"
                )
                return True

            if self._max_spool_messages == 0 or self._max_spool_bytes == 0:
                logger.warning(
                    f"Dropping event {event.event_id}: memory queue full "
                    f"({len(self._memory_queue)}/{self._max_memory_queue}) "
                    "and spooling is disabled"
                )
                return False

            return await self._spool_event(event)

    async def _spool_event(self, event: ModelQueuedEvent) -> bool:
        """Spool an event to disk. Caller must hold self._lock."""
        if self._max_spool_messages == 0 or self._max_spool_bytes == 0:
            return False

        try:
            event_json = event.model_dump_json()
            event_bytes = len(event_json.encode("utf-8"))
        except Exception:
            logger.exception("Failed to serialize event %s", event.event_id)
            return False

        while (
            len(self._spool_files) >= self._max_spool_messages
            or self._spool_bytes + event_bytes > self._max_spool_bytes
        ) and self._spool_files:
            await self._drop_oldest_spool()

        if event_bytes > self._max_spool_bytes:
            logger.warning(
                "Dropping event %s: serialized size (%d bytes) exceeds max_spool_bytes (%d)",
                event.event_id,
                event_bytes,
                self._max_spool_bytes,
            )
            return False

        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        filename = f"{timestamp}_{event.event_id}.json"
        filepath = self._spool_dir / filename

        try:
            filepath.write_text(event_json, encoding="utf-8")
            self._spool_files.append(filepath)
            self._spool_bytes += event_bytes
            logger.debug(
                f"Event {event.event_id} spooled to disk "
                f"(spool: {len(self._spool_files)}/{self._max_spool_messages}, "
                f"bytes: {self._spool_bytes}/{self._max_spool_bytes})"
            )
            return True
        except OSError:
            logger.exception("Failed to write spool file %s", filepath)
            return False

    async def _drop_oldest_spool(self) -> None:
        """Drop the oldest spooled event. Caller must hold self._lock."""
        if not self._spool_files:
            return

        oldest = self._spool_files.popleft()
        try:
            file_size = oldest.stat().st_size
            oldest.unlink()
            self._spool_bytes = max(0, self._spool_bytes - file_size)
            event_id = (
                oldest.stem.split("_", 1)[1] if "_" in oldest.stem else oldest.stem
            )
            logger.warning(
                f"Dropping oldest spooled event {event_id} due to spool overflow"
            )
        except OSError:
            logger.exception("Failed to delete oldest spool file %s", oldest)

    async def dequeue(self) -> ModelQueuedEvent | None:
        """Return the next event to publish, preferring the durable outbox.

        Duty-critical outbox events are returned via peek (not removed) and
        tracked as in-flight; the caller MUST call ``ack(event)`` after a
        confirmed publish to truncate the outbox record. If publish fails, the
        event stays durable and is re-served on the next dequeue.
        """
        record = await self._outbox.peek()
        if record is not None:
            self._in_flight_outbox[record.event.event_id] = record
            logger.debug(
                "Dequeued duty-critical event %s from outbox (pending: %d)",
                record.event.event_id,
                self._outbox.pending_count(),
            )
            return record.event

        async with self._lock:
            if self._memory_queue:
                event = self._memory_queue.popleft()
                logger.debug(
                    f"Dequeued event {event.event_id} from memory "
                    f"(remaining: {len(self._memory_queue)})"
                )
                return event

            if self._spool_files:
                return await self._dequeue_from_spool()

            return None

    async def ack(self, event: ModelQueuedEvent) -> None:
        """Acknowledge a published event.

        For duty-critical events this truncates the outbox record (truncate-on-
        ack). For telemetry events it is a no-op -- those entries are removed
        from memory/spool at dequeue time.
        """
        record = self._in_flight_outbox.pop(event.event_id, None)
        if record is not None:
            from omnimarket.nodes.node_emit_daemon.durable_outbox import OutboxRecord

            assert isinstance(record, OutboxRecord)
            await self._outbox.ack(record)

    async def _dequeue_from_spool(self) -> ModelQueuedEvent | None:
        """Dequeue next event from disk spool. Caller must hold self._lock."""
        if not self._spool_files:
            return None

        filepath = self._spool_files.popleft()
        try:
            content = filepath.read_text(encoding="utf-8")
            event = ModelQueuedEvent.model_validate_json(content)
            try:
                file_size = filepath.stat().st_size
            except OSError:
                file_size = len(content.encode("utf-8"))
            self._spool_bytes = max(0, self._spool_bytes - file_size)
        except OSError:
            logger.exception("Failed to read spool file %s", filepath)
            with contextlib.suppress(OSError):
                filepath.unlink()
            return None
        except Exception:
            logger.exception("Failed to parse spool file %s", filepath)
            try:
                file_size = filepath.stat().st_size
            except OSError:
                file_size = 0
            self._spool_bytes = max(0, self._spool_bytes - file_size)
            with contextlib.suppress(OSError):
                filepath.unlink()
            return None

        try:
            filepath.unlink()
        except OSError:
            logger.warning(
                "Failed to delete spool file %s after successful dequeue",
                filepath,
            )

        logger.debug(
            f"Dequeued event {event.event_id} from spool "
            f"(remaining spool: {len(self._spool_files)})"
        )
        return event

    def memory_size(self) -> int:
        return len(self._memory_queue)

    def spool_size(self) -> int:
        return len(self._spool_files)

    def total_size(self) -> int:
        """Bounded (telemetry) backlog size: memory + spool. Excludes outbox."""
        return self.memory_size() + self.spool_size()

    def outbox_pending(self) -> int:
        """Count of pending duty-critical events in the durable outbox."""
        return self._outbox.pending_count()

    def outbox_pending_bytes(self) -> int:
        return self._outbox.pending_bytes()

    async def load_outbox(self) -> int:
        """Restore pending duty-critical events from the durable outbox on startup."""
        return await self._outbox.load_pending()

    async def drain_to_spool(self) -> int:
        async with self._lock:
            if self._max_spool_messages == 0 or self._max_spool_bytes == 0:
                memory_count = len(self._memory_queue)
                if memory_count > 0:
                    logger.warning(
                        f"Spooling disabled. {memory_count} events in memory will be lost."
                    )
                return 0

            count = 0
            while self._memory_queue:
                event = self._memory_queue.popleft()
                if await self._spool_event(event):
                    count += 1
                else:
                    logger.error(f"Failed to spool event {event.event_id} during drain")
            logger.info(f"Drained {count} events from memory to spool")
            return count

    async def load_spool(self) -> int:
        async with self._lock:
            self._spool_files.clear()
            self._spool_bytes = 0

            if not self._spool_dir.exists():
                return 0

            try:
                files = sorted(self._spool_dir.glob("*.json"))
                for filepath in files:
                    try:
                        file_size = filepath.stat().st_size
                        self._spool_files.append(filepath)
                        self._spool_bytes += file_size
                    except OSError as e:
                        logger.warning(f"Failed to stat spool file {filepath}: {e}")

                count = len(self._spool_files)
                if count > 0:
                    logger.info(
                        f"Loaded {count} events from spool ({self._spool_bytes} bytes)"
                    )
                return count
            except OSError:
                logger.exception("Failed to scan spool directory")
                return 0


__all__: list[str] = ["BoundedEventQueue", "ModelQueuedEvent"]
