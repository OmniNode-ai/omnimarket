# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the host disk orchestrator holds between events: the newest reading per host and the open runs (OMN-23866)."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from omnimarket.models.model_host_disk_decision import ModelHostDiskHostReading
from omnimarket.models.model_host_disk_orchestrator import (
    ModelHostDiskCommandOutcome,
    ModelHostDiskEvictRequested,
    ModelHostDiskReadingSummary,
    ModelHostDiskRunCompleted,
)
from omnimarket.models.model_host_disk_reading import ModelHostDiskRead


class ProtocolHostDiskRows(Protocol):
    """Where the run reads each host's newest reading. The projection's rows are the production source."""

    def snapshot(self) -> tuple[ModelHostDiskHostReading, ...]: ...


class InMemoryHostDiskRows:
    """Thread-safe, bounded store of the newest reading per host, fed by the host disk read event."""

    def __init__(self, max_hosts: int = 256) -> None:
        self._max_hosts = max_hosts
        self._lock = threading.Lock()
        self._rows: OrderedDict[str, ModelHostDiskHostReading] = OrderedDict()

    def record_read(self, read: ModelHostDiskRead) -> None:
        with self._lock:
            held = self._rows.get(read.host_name)
            if held is not None and read.read_at <= held.read_at:
                return
            self._rows[read.host_name] = ModelHostDiskHostReading(
                host_name=read.host_name,
                read_at=read.read_at,
                volumes=read.volumes,
                classes=read.classes,
            )
            self._rows.move_to_end(read.host_name)
            while len(self._rows) > self._max_hosts:
                self._rows.popitem(last=False)

    def snapshot(self) -> tuple[ModelHostDiskHostReading, ...]:
        with self._lock:
            return tuple(self._rows.values())


@dataclass
class OpenRun:
    """A run whose commands are out and whose results are still being collected."""

    run: ModelHostDiskRunCompleted
    deadline_at: datetime
    outcomes: dict[str, ModelHostDiskCommandOutcome] = field(default_factory=dict)

    def waiting_for(self) -> tuple[ModelHostDiskEvictRequested, ...]:
        return tuple(
            c for c in self.run.commands if c.idempotency_key not in self.outcomes
        )


class RunBook:
    """Open runs by run id, bounded; a result is matched to its run by the command's idempotency key."""

    def __init__(self, max_open: int = 16) -> None:
        self._max_open = max_open
        self._lock = threading.Lock()
        self._open: OrderedDict[str, OpenRun] = OrderedDict()

    def open(self, run: OpenRun) -> None:
        with self._lock:
            self._open[run.run.run_id] = run
            while len(self._open) > self._max_open:
                self._open.popitem(last=False)

    def record(self, outcome: ModelHostDiskCommandOutcome) -> OpenRun | None:
        """Attach the outcome to the open run that sent its key; return that run once nothing is awaited."""
        with self._lock:
            for open_run in self._open.values():
                if any(
                    c.idempotency_key == outcome.idempotency_key
                    for c in open_run.run.commands
                ):
                    open_run.outcomes.setdefault(outcome.idempotency_key, outcome)
                    if not open_run.waiting_for():
                        del self._open[open_run.run.run_id]
                        return open_run
                    return None
            return None

    def overdue(self, now: datetime) -> tuple[OpenRun, ...]:
        with self._lock:
            due = tuple(o for o in self._open.values() if now >= o.deadline_at)
            for o in due:
                del self._open[o.run.run_id]
            return due

    def open_count(self) -> int:
        with self._lock:
            return len(self._open)


@dataclass
class HostDiskOrchestratorState:
    """The orchestrator's held state; handlers share one instance, and a test builds its own."""

    rows: ProtocolHostDiskRows = field(default_factory=InMemoryHostDiskRows)
    runs: RunBook = field(default_factory=RunBook)


def summarise(
    readings: tuple[ModelHostDiskHostReading, ...], volume_for: dict[str, str]
) -> tuple[ModelHostDiskReadingSummary, ...]:
    out: list[ModelHostDiskReadingSummary] = []
    for r in sorted(readings, key=lambda x: x.host_name):
        path = volume_for.get(r.host_name)
        volume = next((v for v in r.volumes if v.path == path), None)
        out.append(
            ModelHostDiskReadingSummary(
                host_name=r.host_name,
                read_at=r.read_at,
                free_bytes=None if volume is None else volume.free_bytes,
            )
        )
    return tuple(out)
