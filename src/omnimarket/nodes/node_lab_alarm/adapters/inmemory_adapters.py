# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory backend, the default: fixtures for the manifest, the broker's lag and the sample."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_effects_held import (
    ModelEffectsGroupReading,
    ModelEscalationBrief,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    EffectsGroupReadError,
    GroupNeverCommittedError,
    GroupUnreadableError,
    LaneDispatchError,
    ManifestUnreachableError,
    ManifestUnreadableError,
    RunRecordUnwritableError,
)


class InMemoryManifest:
    """Serves one manifest payload, or refuses with ``unreadable`` or, as an endpoint that is down, ``unreachable``."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.unreadable: str | None = None
        self.unreachable: str | None = None

    def read(self) -> Mapping[str, Any]:
        if self.unreachable is not None:
            raise ManifestUnreachableError(self.unreachable)
        if self.unreadable is not None:
            raise ManifestUnreadableError(self.unreadable)
        return self.payload


class InMemoryManifestCache:
    def __init__(self, entry: tuple[Mapping[str, Any], datetime] | None = None) -> None:
        self.entry = entry

    def load(self) -> tuple[Mapping[str, Any], datetime] | None:
        return self.entry

    def save(self, manifest: Mapping[str, Any], fetched_at: datetime) -> None:
        self.entry = (dict(manifest), fetched_at)


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


class InMemoryEffectsGroups:
    """Serves the readings it holds, or refuses with ``unreadable``."""

    def __init__(self, readings: tuple[ModelEffectsGroupReading, ...]) -> None:
        self.readings = readings
        self.unreadable: str | None = None

    def read(self) -> tuple[ModelEffectsGroupReading, ...]:
        if self.unreadable is not None:
            raise EffectsGroupReadError(self.unreadable)
        return self.readings


class InMemoryHeldState:
    def __init__(self, state: Mapping[str, str] | None = None) -> None:
        self.state: dict[str, str] = dict(state or {})

    def load(self) -> dict[str, str]:
        return dict(self.state)

    def save(self, state: Mapping[str, str]) -> None:
        self.state = dict(state)


class InMemoryDispatcher:
    """Records every brief it is given instead of starting a lane; ``refuse`` makes it raise."""

    def __init__(self) -> None:
        self.briefs: list[ModelEscalationBrief] = []
        self.refuse: str | None = None

    def dispatch(self, brief: ModelEscalationBrief) -> None:
        if self.refuse is not None:
            raise LaneDispatchError(self.refuse)
        self.briefs.append(brief)


__all__ = [
    "InMemoryDispatcher",
    "InMemoryEffectsGroups",
    "InMemoryHeldState",
    "InMemoryLag",
    "InMemoryManifest",
    "InMemoryManifestCache",
    "InMemoryRunRecords",
    "InMemorySamples",
]
