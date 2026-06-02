# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Append-only durable outbox for duty-critical emit-daemon events.

Duty-critical commands and evidence (e.g. delegation-request, dod-verify-completed,
audit-scope-violation, session-outcome, intent-commit-bound) must never be
dropped on the producer edge. They are persisted here append-only and replayed
to Kafka with truncate-on-ack semantics:

    1. ``append`` writes one event per file and records it as pending.
    2. ``peek`` returns the oldest pending record (FIFO) without removing it.
    3. ``ack`` truncates (deletes) the record's file ONLY after a confirmed
       Kafka publish. Until then the event survives a daemon restart.
    4. ``load_pending`` re-scans the outbox dir on startup, restoring the
       pending set so replay resumes after a crash or restart.

When the outbox is full (by message count or bytes), ``append`` raises
``DurableOutboxFullError`` -- it never drops. The emit path surfaces this to the
caller as explicit backpressure / degraded mode.

Concurrency: coroutine-safe via asyncio.Lock (not thread-safe).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_emit_daemon.event_queue import ModelQueuedEvent
from omnimarket.nodes.node_emit_daemon.models.model_durability import (
    DurableOutboxFullError,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_OUTBOX_MESSAGES: int = 100_000
DEFAULT_MAX_OUTBOX_BYTES: int = 268_435_456  # 256 MB


@dataclass(frozen=True)
class OutboxRecord:
    """A pending duty-critical event persisted in the outbox."""

    event: ModelQueuedEvent
    path: Path
    size_bytes: int


class DurableOutbox:
    """Append-only durable outbox with truncate-on-ack replay semantics.

    Args:
        outbox_dir: Directory holding one JSON file per pending event.
        max_messages: Hard cap on pending message count. On overflow, append
            raises DurableOutboxFullError (never drops).
        max_bytes: Hard cap on total pending bytes. On overflow, append raises
            DurableOutboxFullError (never drops).
    """

    def __init__(
        self,
        outbox_dir: Path,
        max_messages: int = DEFAULT_MAX_OUTBOX_MESSAGES,
        max_bytes: int = DEFAULT_MAX_OUTBOX_BYTES,
    ) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be >= 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        self._outbox_dir = outbox_dir
        self._max_messages = max_messages
        self._max_bytes = max_bytes
        self._pending: deque[OutboxRecord] = deque()
        self._pending_bytes: int = 0
        self._lock = asyncio.Lock()
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        # Fail fast: a duty-critical outbox that cannot create its directory is
        # not safe to operate -- never silently degrade to in-memory loss.
        self._outbox_dir.mkdir(parents=True, exist_ok=True)

    async def append(self, event: ModelQueuedEvent) -> OutboxRecord:
        """Persist a duty-critical event append-only. Raise if the outbox is full."""
        async with self._lock:
            event_json = event.model_dump_json()
            event_bytes = len(event_json.encode("utf-8"))

            if event_bytes > self._max_bytes:
                raise DurableOutboxFullError(
                    f"Event {event.event_id} ({event_bytes} bytes) exceeds outbox "
                    f"capacity ({self._max_bytes} bytes); cannot persist duty-critical event"
                )
            if len(self._pending) >= self._max_messages:
                raise DurableOutboxFullError(
                    f"Outbox full: {len(self._pending)}/{self._max_messages} pending "
                    f"duty-critical events; refusing to drop event {event.event_id}"
                )
            if self._pending_bytes + event_bytes > self._max_bytes:
                raise DurableOutboxFullError(
                    f"Outbox byte cap reached: {self._pending_bytes}+{event_bytes} > "
                    f"{self._max_bytes}; refusing to drop event {event.event_id}"
                )

            timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
            filename = f"{timestamp}_{event.event_id}.json"
            filepath = self._outbox_dir / filename
            filepath.write_text(event_json, encoding="utf-8")

            record = OutboxRecord(event=event, path=filepath, size_bytes=event_bytes)
            self._pending.append(record)
            self._pending_bytes += event_bytes
            logger.debug(
                "Outbox appended duty-critical event %s (pending=%d, bytes=%d)",
                event.event_id,
                len(self._pending),
                self._pending_bytes,
            )
            return record

    async def peek(self) -> OutboxRecord | None:
        """Return the oldest pending record (FIFO) without removing it."""
        async with self._lock:
            if not self._pending:
                return None
            return self._pending[0]

    async def ack(self, record: OutboxRecord) -> None:
        """Truncate (delete) a record's file after a confirmed publish.

        Removes the record from the pending set. Idempotent: acking a record
        that is no longer pending is a no-op.
        """
        async with self._lock:
            try:
                self._pending.remove(record)
            except ValueError:
                return
            self._pending_bytes = max(0, self._pending_bytes - record.size_bytes)
            try:
                record.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception("Failed to truncate acked outbox file %s", record.path)
            logger.debug(
                "Outbox acked event %s (pending=%d)",
                record.event.event_id,
                len(self._pending),
            )

    async def load_pending(self) -> int:
        """Re-scan the outbox dir and restore pending records (startup replay)."""
        async with self._lock:
            self._pending.clear()
            self._pending_bytes = 0
            if not self._outbox_dir.exists():
                return 0
            for filepath in sorted(self._outbox_dir.glob("*.json")):
                try:
                    content = filepath.read_text(encoding="utf-8")
                    event = ModelQueuedEvent.model_validate_json(content)
                except (OSError, ValueError):
                    logger.exception("Corrupt outbox file %s; quarantining", filepath)
                    self._quarantine(filepath)
                    continue
                size_bytes = len(content.encode("utf-8"))
                self._pending.append(
                    OutboxRecord(event=event, path=filepath, size_bytes=size_bytes)
                )
                self._pending_bytes += size_bytes
            count = len(self._pending)
            if count > 0:
                logger.info(
                    "Outbox loaded %d pending duty-critical events (%d bytes)",
                    count,
                    self._pending_bytes,
                )
            return count

    def _quarantine(self, filepath: Path) -> None:
        """Move an unparseable file aside so it never blocks replay."""
        try:
            quarantine_dir = self._outbox_dir / "corrupt"
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            filepath.rename(quarantine_dir / filepath.name)
        except OSError:
            logger.exception("Failed to quarantine corrupt outbox file %s", filepath)

    def pending_count(self) -> int:
        return len(self._pending)

    def pending_bytes(self) -> int:
        return self._pending_bytes


__all__: list[str] = [
    "DEFAULT_MAX_OUTBOX_BYTES",
    "DEFAULT_MAX_OUTBOX_MESSAGES",
    "DurableOutbox",
    "OutboxRecord",
]
