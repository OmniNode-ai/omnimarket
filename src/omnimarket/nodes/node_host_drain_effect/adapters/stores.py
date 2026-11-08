# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drain stores: in memory (the default) and a durable JSON-lines file.

Every store keeps the latest record per host. A record that does not parse is skipped and counted, never fixed.

There is no broker-backed store here: the injected event bus has no read-from-the-start snapshot of a topic, and a raw
Kafka client in a node bypasses it (the imperative contract guard refuses one). A drain state shared across hosts
belongs to a bus projection, a node of its own.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class InMemoryDrainStore:
    def __init__(self) -> None:
        self._draining: dict[str, bool] = {}

    def set_draining(
        self, host_name: str, draining: bool, *, reason: str, at: datetime
    ) -> None:
        self._draining[host_name] = draining

    def is_draining(self, host_name: str) -> bool:
        return self._draining.get(host_name, False)

    def draining_hosts(self) -> tuple[str, ...]:
        return tuple(sorted(h for h, d in self._draining.items() if d))


def _record(
    host_name: str, draining: bool, reason: str, at: datetime
) -> dict[str, object]:
    return {
        "host": host_name,
        "draining": draining,
        "reason": reason,
        "at": at.isoformat(),
    }


def _fold(records: list[dict[str, object]]) -> dict[str, bool]:
    state: dict[str, bool] = {}
    for rec in records:
        host = rec.get("host")
        flag = rec.get("draining")
        if isinstance(host, str) and isinstance(flag, bool):
            state[host] = flag
    return state


class JsonlDrainStore:
    """Append-only file of drain records; the state is the fold of the file, so a restart keeps it."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.skipped_lines = 0

    def _read(self) -> dict[str, bool]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        records: list[dict[str, object]] = []
        self.skipped_lines = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                self.skipped_lines += 1
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                self.skipped_lines += 1
        return _fold(records)

    def set_draining(
        self, host_name: str, draining: bool, *, reason: str, at: datetime
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_record(host_name, draining, reason, at)) + "\n")

    def is_draining(self, host_name: str) -> bool:
        return self._read().get(host_name, False)

    def draining_hosts(self) -> tuple[str, ...]:
        return tuple(sorted(h for h, d in self._read().items() if d))
