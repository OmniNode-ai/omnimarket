# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory default seams: the same protocols as the real ones, with no repository behind them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumEntityState,
    ModelCatalogRow,
    ModelInMemoryBackend,
    ModelQuarantineEvent,
    ModelQuarantineRecord,
)
from omnimarket.nodes.node_code_quarantine_effect.protocols import QuarantineStepError


class InMemoryStore:
    def __init__(self) -> None:
        #: An injected break: while set, every read of the store fails as a down store does.
        self.down = False
        self.records: dict[tuple[str, str], ModelQuarantineRecord] = {}
        self.catalog: list[tuple[str, ModelCatalogRow]] = []

    def get(self, repository: str, entity: str) -> ModelQuarantineRecord | None:
        if self.down:
            raise QuarantineStepError("store", "store is down")
        return self.records.get((repository, entity))

    def put(self, record: ModelQuarantineRecord) -> None:
        self.records[(record.repository, record.entity)] = record

    def excluded_paths(self, repository: str) -> tuple[str, ...]:
        paths = {
            path
            for (repo, _), record in self.records.items()
            if repo == repository and record.state is EnumEntityState.QUARANTINED
            for path in (*record.entity_paths, *record.test_paths)
        }
        return tuple(sorted(paths))

    def append_catalog(
        self, repository: str, rows: tuple[ModelCatalogRow, ...]
    ) -> None:
        self.catalog.extend((repository, row) for row in rows)


class InMemoryRepository:
    def __init__(
        self, files: dict[str, set[str]], tags: dict[str, dict[str, str]]
    ) -> None:
        self.files = files
        self.tags = tags

    @classmethod
    def from_overlay(cls, spec: ModelInMemoryBackend) -> InMemoryRepository:
        return cls(
            {repo: set(paths) for repo, paths in spec.files.items()},
            {repo: dict(tags) for repo, tags in spec.tags.items()},
        )

    def path_exists(self, repository: str, path: str) -> bool:
        return path in self.files.get(repository, set())

    def tag_sha(self, repository: str, tag: str) -> str:
        return self.tags.get(repository, {}).get(tag, "")

    def remove_paths(self, repository: str, paths: tuple[str, ...]) -> None:
        self.files.setdefault(repository, set()).difference_update(paths)


class InMemoryGates:
    """Each gate reads a settable flag; the excluded paths each read saw are kept."""

    def __init__(self, *, build: bool, start: bool, reading: bool) -> None:
        self.build = build
        self.start = start
        self.reading = reading
        self.seen_excluded: list[tuple[str, ...]] = []

    @classmethod
    def from_overlay(cls, spec: ModelInMemoryBackend) -> InMemoryGates:
        return cls(
            build=spec.clean_build_green,
            start=spec.lab_runtime_start_green,
            reading=spec.reading_present,
        )

    def clean_build(self, repository: str, excluded: tuple[str, ...]) -> bool:
        self.seen_excluded.append(excluded)
        return self.build

    def lab_runtime_start(self, repository: str, excluded: tuple[str, ...]) -> bool:
        self.seen_excluded.append(excluded)
        return self.start

    def reading_present(self, repository: str, entity: str) -> bool:
        return self.reading


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SteppedClock:
    """A clock that only moves when told to; the in-memory suite uses it to run 7 days in a test."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> None:
        self._now += timedelta(**delta)


class InMemoryReporter:
    def __init__(self) -> None:
        self.events: list[ModelQuarantineEvent] = []

    def report(self, event: ModelQuarantineEvent) -> None:
        self.events.append(event)
