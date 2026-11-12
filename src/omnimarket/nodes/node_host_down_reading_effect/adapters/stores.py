# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stores of host-down readings: in-memory and append-only JSON lines."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from omnimarket.nodes.node_host_down_compute.models.model_host_down import (
    EnumHostState,
)
from omnimarket.nodes.node_host_down_reading_effect.models.model_host_down_reading import (
    ModelHostDownRecord,
)


class InMemoryDownStore:
    """The in-memory down store: one record per host, the newest wins."""

    def __init__(self) -> None:
        self._latest: dict[str, ModelHostDownRecord] = {}

    def write(self, record: ModelHostDownRecord) -> None:
        self._latest[record.host_name] = record

    def latest(self, host_name: str) -> ModelHostDownRecord | None:
        return self._latest.get(host_name)

    def down_hosts(self, as_of: datetime, max_age_s: int) -> tuple[str, ...]:
        return tuple(
            host
            for host, record in sorted(self._latest.items())
            if record.state == EnumHostState.DOWN
            and 0 <= (as_of - record.at).total_seconds() <= max_age_s
        )


class JsonlDownStore:
    """Append-only JSON lines: one line per record, the latest per host is the fold."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, record: ModelHostDownRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")

    def latest(self, host_name: str) -> ModelHostDownRecord | None:
        if not self._path.exists():
            return None
        records: dict[str, ModelHostDownRecord] = {}
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = ModelHostDownRecord.model_validate_json(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                records[record.host_name] = record
        return records.get(host_name)

    def down_hosts(self, as_of: datetime, max_age_s: int) -> tuple[str, ...]:
        if not self._path.exists():
            return ()
        records: dict[str, ModelHostDownRecord] = {}
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = ModelHostDownRecord.model_validate_json(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                records[record.host_name] = record
        return tuple(
            host
            for host, record in sorted(records.items())
            if record.state == EnumHostState.DOWN
            and 0 <= (as_of - record.at).total_seconds() <= max_age_s
        )
