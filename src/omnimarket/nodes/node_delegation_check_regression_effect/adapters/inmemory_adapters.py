# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory ports for tests and for a replay of recorded results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from pydantic import BaseModel

from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelChange,
    ModelProbeRun,
    ModelRebuildCompletedObserved,
    ModelWatchedCheck,
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProbeRunsUnreadableError,
)


class InMemoryRunSource:
    """Recorded runs by check id; a replay clock hides every run finished after it."""

    def __init__(
        self,
        runs: Mapping[str, Sequence[ModelProbeRun]],
        *,
        until: datetime | None = None,
        unreadable: Mapping[str, str] | None = None,
    ) -> None:
        self._runs = {k: tuple(v) for k, v in runs.items()}
        self._until = until
        self._unreadable = dict(unreadable or {})

    def runs(self, check: ModelWatchedCheck, limit: int) -> tuple[ModelProbeRun, ...]:
        if check.check_id in self._unreadable:
            raise ProbeRunsUnreadableError(self._unreadable[check.check_id])
        recorded = self._runs.get(check.check_id, ())
        visible = [r for r in recorded if self._until is None or r.at <= self._until]
        return tuple(sorted(visible, key=lambda r: r.at, reverse=True)[:limit])


class InMemoryChangeSource:
    def __init__(self, changes: Sequence[ModelChange] = ()) -> None:
        self._changes = tuple(changes)

    def between(self, start: datetime, end: datetime) -> tuple[ModelChange, ...]:
        return tuple(
            sorted(
                (c for c in self._changes if start < c.at <= end), key=lambda c: c.at
            )
        )


class InMemoryClaims:
    def __init__(self, held: Mapping[str, str] | None = None) -> None:
        self._held = dict(held or {})

    def holder(self, check_id: str) -> str | None:
        return self._held.get(check_id)


class InMemorySeenStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def seen(self, event_id: str) -> bool:
        return event_id in self._seen

    def record(self, event_id: str) -> None:
        self._seen.add(event_id)


class RecordingPublisher:
    """Keeps every published payload; ``fail_topics`` names topics whose publish raises OSError."""

    def __init__(self, fail_topics: Sequence[str] = ()) -> None:
        self.published: list[tuple[str, str, BaseModel]] = []
        self._fail = frozenset(fail_topics)

    def publish(self, topic: str, key: str, payload: BaseModel) -> None:
        if topic in self._fail:
            raise OSError(f"publish refused: {topic}")
        self.published.append((topic, key, payload))

    def on(self, topic: str) -> list[BaseModel]:
        return [p for t, _, p in self.published if t == topic]


class InMemoryDevBuildStore:
    def __init__(self) -> None:
        self.builds: list[ModelRebuildCompletedObserved] = []

    def append(self, build: ModelRebuildCompletedObserved) -> None:
        self.builds.append(build)
