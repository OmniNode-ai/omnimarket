# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory backend, the default: fixtures for the manifest, the broker's lag and the sample."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.nodes.node_lab_alarm.protocols import (
    GroupNeverCommittedError,
    GroupUnreadableError,
    ManifestUnreadableError,
    RunRecordUnwritableError,
)


class InMemoryManifest:
    """Serves one manifest payload, or refuses with ``unreadable``."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.unreadable: str | None = None

    def read(self) -> Mapping[str, Any]:
        if self.unreadable is not None:
            raise ManifestUnreadableError(self.unreadable)
        return self.payload


class InMemoryLag:
    """Total lag per group; a group in ``never_committed`` or ``refused`` raises its typed error."""

    def __init__(self, lags: Mapping[str, int]) -> None:
        self.lags: dict[str, int] = dict(lags)
        self.never_committed: set[str] = set()
        self.refused: dict[str, str] = {}

    def total_lag(self, group: str) -> int:
        if group in self.refused:
            raise GroupUnreadableError(self.refused[group])
        if group in self.never_committed or group not in self.lags:
            raise GroupNeverCommittedError(
                f"group {group} holds no committed offset on any partition"
            )
        return self.lags[group]


class InMemorySamples:
    def __init__(self, sample: Mapping[str, int] | None = None) -> None:
        self.sample: dict[str, int] | None = None if sample is None else dict(sample)

    def load(self) -> dict[str, int] | None:
        return None if self.sample is None else dict(self.sample)

    def save(self, sample: Mapping[str, int]) -> None:
        self.sample = dict(sample)


class InMemoryRunRecords:
    """Keeps the appended run records in a list; ``unwritable`` makes the next append refuse."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.unwritable: str | None = None

    def append(self, record: Mapping[str, Any]) -> None:
        if self.unwritable is not None:
            raise RunRecordUnwritableError(self.unwritable)
        self.records.append(dict(record))


__all__ = ["InMemoryLag", "InMemoryManifest", "InMemoryRunRecords", "InMemorySamples"]
