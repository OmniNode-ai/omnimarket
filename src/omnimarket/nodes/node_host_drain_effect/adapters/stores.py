# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drain stores: in memory (the default) and a durable JSON-lines file.

Every store keeps the latest record per host. A record that does not parse is skipped and counted, never fixed.
A drain mark is time-bounded: a draining record whose ``until`` is at or behind the store's clock reads as not
draining, in ``is_draining``, in ``draining_hosts`` and in ``until_of``. A record whose ``until`` does not parse is
skipped and counted, never read as draining forever.

There is no broker-backed store here: the injected event bus has no read-from-the-start snapshot of a topic, and a raw
Kafka client in a node bypasses it (the imperative contract guard refuses one). A drain state shared across hosts
belongs to a bus projection, a node of its own.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    ModelDrainMark,
)

#: A record per host: whether it reads draining and when its mark expires (None: no expiry).
Record = tuple[bool, datetime | None]


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """A naive stamp reads as UTC, so it never compares against an aware clock."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _reads_draining(record: Record | None, now: datetime) -> bool:
    """Whether the record reads as draining now: draining, and not past its until."""
    if record is None:
        return False
    draining, until = record
    return draining and (until is None or now < _as_utc(until))


class InMemoryDrainStore:
    #: A mark here dies with the process: it can never back a SAFE reading.
    durable = False

    def __init__(self, *, clock: Callable[[], datetime] = _now) -> None:
        self._clock = clock
        self._records: dict[str, Record] = {}

    def set_draining(
        self,
        host_name: str,
        draining: bool,
        *,
        reason: str,
        at: datetime,
        until: datetime | None = None,
    ) -> None:
        self._records[host_name] = (draining, until)

    def is_draining(self, host_name: str) -> bool:
        return _reads_draining(self._records.get(host_name), self._clock())

    def draining_hosts(self) -> tuple[str, ...]:
        now = self._clock()
        return tuple(
            sorted(
                h for h, record in self._records.items() if _reads_draining(record, now)
            )
        )

    def until_of(self, host_name: str) -> datetime | None:
        """The mark's until while the host reads draining, else None."""
        record = self._records.get(host_name)
        if record is None or not _reads_draining(record, self._clock()):
            return None
        return None if record[1] is None else _as_utc(record[1])

    def mark_of(self, host_name: str) -> ModelDrainMark | None:
        """The host's record as held, whether or not it is past its until."""
        record = self._records.get(host_name)
        return (
            None
            if record is None
            else ModelDrainMark(draining=record[0], until=record[1])
        )


def _record(
    host_name: str, draining: bool, reason: str, at: datetime, until: datetime | None
) -> dict[str, object]:
    return {
        "host": host_name,
        "draining": draining,
        "reason": reason,
        "at": at.isoformat(),
        "until": None if until is None else until.isoformat(),
    }


def _until_of(parsed: dict[str, object]) -> datetime | None:
    """The record's until; None when it names none. ValueError on one that does not parse."""
    value = parsed.get("until")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("until must be an ISO datetime or null")
    return _as_utc(datetime.fromisoformat(value))


def _state_of(line: str) -> tuple[str, Record] | None:
    """The (host, record) a line holds, or None when the line does not parse."""
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    host = parsed.get("host")
    flag = parsed.get("draining")
    if not isinstance(host, str) or not isinstance(flag, bool):
        return None
    try:
        until = _until_of(parsed)
    except (TypeError, ValueError):
        return None
    return host, (flag, until)


class JsonlDrainStore:
    """Append-only file of drain records; the state is the fold of the file, so a restart keeps it."""

    #: A mark here is a line in a file that outlives the process.
    durable = True

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _now) -> None:
        self._path = path
        self._clock = clock
        self.skipped_lines = 0

    def _read(self) -> dict[str, Record]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        state: dict[str, Record] = {}
        self.skipped_lines = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            state_of = _state_of(line)
            if state_of is None:
                self.skipped_lines += 1
                continue
            host, record = state_of
            state[host] = record
        return state

    def set_draining(
        self,
        host_name: str,
        draining: bool,
        *,
        reason: str,
        at: datetime,
        until: datetime | None = None,
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(_record(host_name, draining, reason, at, until)) + "\n"
            )

    def is_draining(self, host_name: str) -> bool:
        return _reads_draining(self._read().get(host_name), self._clock())

    def draining_hosts(self) -> tuple[str, ...]:
        now = self._clock()
        state = self._read()
        return tuple(
            sorted(h for h, record in state.items() if _reads_draining(record, now))
        )

    def until_of(self, host_name: str) -> datetime | None:
        """The mark's until while the host reads draining, else None."""
        record = self._read().get(host_name)
        if record is None or not _reads_draining(record, self._clock()):
            return None
        return record[1]

    def mark_of(self, host_name: str) -> ModelDrainMark | None:
        """The host's record as held, whether or not it is past its until."""
        record = self._read().get(host_name)
        return (
            None
            if record is None
            else ModelDrainMark(draining=record[0], until=record[1])
        )
