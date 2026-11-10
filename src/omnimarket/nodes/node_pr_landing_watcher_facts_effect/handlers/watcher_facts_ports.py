# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory defaults of the landing watcher facts node's ports (OMN-23507)."""

from __future__ import annotations

from pydantic import BaseModel

from omnimarket.models.landing_decision import ModelLandingFacts


class InMemoryWatcherStateSource:
    """A watcher state, as given."""

    def __init__(self, state: object) -> None:
        self._state = state

    def read_state(self) -> object:
        return self._state


class InMemoryLandingFactsSink:
    """Keeps every facts it is given."""

    def __init__(self) -> None:
        self.written: list[ModelLandingFacts] = []

    def write(self, facts: ModelLandingFacts) -> str:
        self.written.append(facts)
        return "memory"


class InMemoryWatcherFactsPublisher:
    """Records the events it is given, with their topic."""

    def __init__(self) -> None:
        self.published: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        self.published.append((topic, event))


__all__: list[str] = [
    "InMemoryLandingFactsSink",
    "InMemoryWatcherFactsPublisher",
    "InMemoryWatcherStateSource",
]
