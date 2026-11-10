# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory defaults of the landing snapshot step's ports (OMN-23409)."""

from __future__ import annotations

from pydantic import BaseModel

from omnimarket.models.landing_decision import ModelLandingFacts
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    ModelLandingTickRecord,
    ModelPrWriteFacts,
)


class InMemoryLandingFactsSource:
    """Facts per tick, as given."""

    name = "inmemory-facts"

    def __init__(self, facts: dict[int, ModelLandingFacts]) -> None:
        self._facts = dict(facts)

    def read_facts(self, tick: int) -> ModelLandingFacts | None:
        return self._facts.get(tick)


class InMemoryLandingWriteFactsSource:
    """Write facts per PR key and head, as given; a PR it holds none for at that head is absent."""

    name = "inmemory-write-facts"

    def __init__(self, facts: tuple[ModelPrWriteFacts, ...]) -> None:
        self._facts = {(f.pr, f.head_sha): f for f in facts}
        self.reads: list[dict[str, str]] = []

    def read_write_facts(self, heads: dict[str, str]) -> dict[str, ModelPrWriteFacts]:
        self.reads.append(dict(heads))
        return {
            pr: self._facts[(pr, head)]
            for pr, head in heads.items()
            if (pr, head) in self._facts
        }


class InMemoryLandingTickRecorder:
    """Appends every tick's record and keeps them all."""

    def __init__(self) -> None:
        self._records: list[ModelLandingTickRecord] = []

    def record(self, record: ModelLandingTickRecord) -> None:
        self._records.append(record)

    def recorded(self) -> tuple[ModelLandingTickRecord, ...]:
        return tuple(self._records)


class InMemoryLandingSnapshotPublisher:
    """Records the events it is given, with their topic."""

    def __init__(self) -> None:
        self.published: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        self.published.append((topic, event))


__all__: list[str] = [
    "InMemoryLandingFactsSource",
    "InMemoryLandingSnapshotPublisher",
    "InMemoryLandingTickRecorder",
    "InMemoryLandingWriteFactsSource",
]
