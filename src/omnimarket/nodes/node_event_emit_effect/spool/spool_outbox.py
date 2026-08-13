# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""File-based spool outbox for ``node_event_emit_effect`` (OMN-15965 R1).

The hook side appends events to a local spool (fast, never blocks on the
network); the effect node's ``handle()`` drains the spool (publish current +
backlog) opportunistically on invocation. This module owns the on-disk
format and the two-tier bounded policy only -- publish/drain sequencing
lives in ``handlers/handler_event_emit_effect.py``.

Format: one JSON file per pending event, filename
``{monotonic_seq:020d}_{event_id}.json``, sorted lexically for FIFO.
Contents: ``event_id``, ``event_type``, ``topics``, ``tier``, ``payload``,
``partition_key``, ``correlation_id``, ``queued_at`` (UTC, tz-aware).

Bounded policy (two tiers, per-topic ``tier`` from
``spool/topic_resolver.py``):
    duty_critical: append-only, never drop. On overflow (message-count or
        byte cap), ``append`` raises :class:`SpoolFullError` -- explicit
        backpressure to the caller, no silent loss.
    telemetry: bounded (message-count and byte caps), drop-oldest on
        overflow, with an explicit dropped count returned from ``append()``.

R1 scope note: this spool is tier-aware and functional but not the
formalized, contract-declared durability-tier surface -- that, plus the two
hard backpressure/degrade proof tests, are R2's job (OMN-15966).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    EnumDurabilityTier,
)

logger = logging.getLogger(__name__)

JsonType = dict[str, object] | list[object] | str | int | float | bool | None

DEFAULT_MAX_DUTY_CRITICAL_MESSAGES = 100_000
DEFAULT_MAX_DUTY_CRITICAL_BYTES = 268_435_456  # 256 MB
DEFAULT_MAX_TELEMETRY_MESSAGES = 1_000
DEFAULT_MAX_TELEMETRY_BYTES = 10_485_760  # 10 MB


class SpoolFullError(RuntimeError):
    """Raised when a duty_critical event cannot be appended: spool is full.

    Mirrors ``node_emit_daemon``'s ``DurableOutboxFullError`` in shape and
    intent but is declared locally -- ``node_event_emit_effect`` must not
    import ``node_emit_daemon``'s Python modules (see
    ``spool/topic_resolver.py`` module docstring).
    """


@dataclass(frozen=True)
class SpoolRecord:
    """A single spooled event, backed by (at most) one file on disk."""

    event_id: str
    event_type: str
    topics: tuple[str, ...]
    tier: EnumDurabilityTier
    payload: JsonType
    partition_key: str | None
    correlation_id: str | None
    queued_at: datetime

    def to_json(self) -> str:
        return json.dumps(
            {
                "event_id": self.event_id,
                "event_type": self.event_type,
                "topics": list(self.topics),
                "tier": self.tier.value,
                "payload": self.payload,
                "partition_key": self.partition_key,
                "correlation_id": self.correlation_id,
                "queued_at": self.queued_at.isoformat(),
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> SpoolRecord:
        data = json.loads(raw)
        queued_at = datetime.fromisoformat(data["queued_at"])
        if queued_at.tzinfo is None:
            queued_at = queued_at.replace(tzinfo=UTC)
        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            topics=tuple(data["topics"]),
            tier=EnumDurabilityTier(data["tier"]),
            payload=data["payload"],
            partition_key=data.get("partition_key"),
            correlation_id=data.get("correlation_id"),
            queued_at=queued_at,
        )


@dataclass(frozen=True)
class SpoolFile:
    """A pending spool record with the on-disk path it was loaded from."""

    record: SpoolRecord
    path: Path


@dataclass(frozen=True)
class AppendOutcome:
    """Result of appending one record to the spool.

    ``spool_file`` is ``None`` only when a single telemetry record's
    serialized size alone exceeds ``max_telemetry_bytes`` -- it can never fit
    even after evicting the entire existing backlog, so it is rejected
    outright (counted in ``dropped_count``) rather than written past the
    configured bound.
    """

    spool_file: SpoolFile | None
    dropped_count: int


_last_seq: int = 0


def _next_monotonic_seq() -> int:
    """Nanosecond-resolution monotonic sequence, safe across process restarts.

    Filenames sort lexically for FIFO; a per-process in-memory counter would
    restart at 0 on every new process and sort new events *before* an
    existing on-disk backlog. Wall-clock nanoseconds are monotonic within a
    host and survive restarts; the ``event_id`` filename suffix keeps
    filenames unique even on a same-nanosecond collision.
    """
    global _last_seq
    seq = time.time_ns()
    if seq <= _last_seq:
        seq = _last_seq + 1
    _last_seq = seq
    return seq


class SpoolOutbox:
    """Two-tier file-based spool: never-drop duty_critical, bounded telemetry."""

    def __init__(
        self,
        spool_dir: Path,
        *,
        max_duty_critical_messages: int = DEFAULT_MAX_DUTY_CRITICAL_MESSAGES,
        max_duty_critical_bytes: int = DEFAULT_MAX_DUTY_CRITICAL_BYTES,
        max_telemetry_messages: int = DEFAULT_MAX_TELEMETRY_MESSAGES,
        max_telemetry_bytes: int = DEFAULT_MAX_TELEMETRY_BYTES,
    ) -> None:
        if max_duty_critical_messages < 1:
            raise ValueError("max_duty_critical_messages must be >= 1")
        if max_duty_critical_bytes < 1:
            raise ValueError("max_duty_critical_bytes must be >= 1")
        if max_telemetry_messages < 1:
            raise ValueError("max_telemetry_messages must be >= 1")
        if max_telemetry_bytes < 1:
            raise ValueError("max_telemetry_bytes must be >= 1")
        self._spool_dir = spool_dir
        self._max_duty_critical_messages = max_duty_critical_messages
        self._max_duty_critical_bytes = max_duty_critical_bytes
        self._max_telemetry_messages = max_telemetry_messages
        self._max_telemetry_bytes = max_telemetry_bytes
        # Fail fast: a spool that cannot create its directory is not safe to
        # operate -- never silently degrade to in-memory-only loss.
        self._spool_dir.mkdir(parents=True, exist_ok=True)

    @property
    def spool_dir(self) -> Path:
        return self._spool_dir

    def _pending_paths(self) -> list[Path]:
        return sorted(self._spool_dir.glob("*.json"))

    def _tier_paths(self, tier: EnumDurabilityTier) -> list[Path]:
        result: list[Path] = []
        for path in self._pending_paths():
            try:
                record = self.read(path)
            except (OSError, ValueError):
                continue
            if record.tier is tier:
                result.append(path)
        return result

    def pending_count(self) -> int:
        return len(self._pending_paths())

    def read(self, path: Path) -> SpoolRecord:
        return SpoolRecord.from_json(path.read_text(encoding="utf-8"))

    def list_pending(self) -> list[SpoolFile]:
        """All pending records, FIFO (oldest first) by filename."""
        files: list[SpoolFile] = []
        for path in self._pending_paths():
            try:
                files.append(SpoolFile(record=self.read(path), path=path))
            except (OSError, ValueError):
                continue
        return files

    def append(self, record: SpoolRecord) -> AppendOutcome:
        if record.tier is EnumDurabilityTier.DUTY_CRITICAL:
            return self._append_duty_critical(record)
        return self._append_telemetry(record)

    def _write(self, record: SpoolRecord) -> Path:
        """Write one spool file atomically: temp file + ``os.replace``.

        An in-place ``write_text`` lets a concurrent reader observe a
        partially written file, or a crash mid-write leave a truncated file
        on disk permanently (``read()``/``list_pending()`` would then swallow
        it as a corrupt record forever). ``os.replace`` is atomic on the same
        filesystem, so readers only ever see the file fully absent or fully
        present.
        """
        body = record.to_json()
        filename = f"{_next_monotonic_seq():020d}_{record.event_id}.json"
        path = self._spool_dir / filename
        tmp_path = path.with_suffix(f".tmp-{os.getpid()}-{_next_monotonic_seq()}")
        tmp_path.write_text(body, encoding="utf-8")
        os.replace(tmp_path, path)
        return path

    def _append_duty_critical(self, record: SpoolRecord) -> AppendOutcome:
        existing = self._tier_paths(EnumDurabilityTier.DUTY_CRITICAL)
        body_bytes = len(record.to_json().encode("utf-8"))
        if len(existing) >= self._max_duty_critical_messages:
            raise SpoolFullError(
                f"Duty-critical spool full: {len(existing)}/"
                f"{self._max_duty_critical_messages} pending; refusing to "
                f"drop event {record.event_id}"
            )
        existing_bytes = sum(p.stat().st_size for p in existing)
        if existing_bytes + body_bytes > self._max_duty_critical_bytes:
            raise SpoolFullError(
                f"Duty-critical spool byte cap reached: "
                f"{existing_bytes}+{body_bytes} > "
                f"{self._max_duty_critical_bytes}; refusing to drop event "
                f"{record.event_id}"
            )
        path = self._write(record)
        return AppendOutcome(
            spool_file=SpoolFile(record=record, path=path), dropped_count=0
        )

    def _append_telemetry(self, record: SpoolRecord) -> AppendOutcome:
        existing = self._tier_paths(EnumDurabilityTier.TELEMETRY)
        existing_bytes = sum(p.stat().st_size for p in existing)
        body_bytes = len(record.to_json().encode("utf-8"))

        # A single record whose own serialized size exceeds the byte cap can
        # never fit -- not even after evicting the entire existing backlog.
        # Reject it outright instead of evicting everything and writing past
        # the configured bound anyway.
        if body_bytes > self._max_telemetry_bytes:
            logger.warning(
                "Dropping oversized telemetry event %s: %d bytes > "
                "max_telemetry_bytes=%d",
                record.event_id,
                body_bytes,
                self._max_telemetry_bytes,
            )
            return AppendOutcome(spool_file=None, dropped_count=1)

        dropped = 0
        while existing and (
            len(existing) >= self._max_telemetry_messages
            or existing_bytes + body_bytes > self._max_telemetry_bytes
        ):
            oldest = existing.pop(0)
            try:
                size = oldest.stat().st_size
                oldest.unlink()
                existing_bytes -= size
                dropped += 1
            except OSError:
                pass
        path = self._write(record)
        return AppendOutcome(
            spool_file=SpoolFile(record=record, path=path), dropped_count=dropped
        )

    def ack(self, path: Path) -> None:
        """Delete a spooled file after a confirmed publish. Idempotent."""
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


__all__: list[str] = [
    "DEFAULT_MAX_DUTY_CRITICAL_BYTES",
    "DEFAULT_MAX_DUTY_CRITICAL_MESSAGES",
    "DEFAULT_MAX_TELEMETRY_BYTES",
    "DEFAULT_MAX_TELEMETRY_MESSAGES",
    "AppendOutcome",
    "JsonType",
    "SpoolFile",
    "SpoolFullError",
    "SpoolOutbox",
    "SpoolRecord",
]
