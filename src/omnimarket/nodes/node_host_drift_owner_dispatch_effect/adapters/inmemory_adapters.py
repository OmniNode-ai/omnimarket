# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory backend, the default: fixed items and live lanes, a dict for the record, recorders for briefs and the runner."""

from __future__ import annotations

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelDriftOpenItem,
    ModelDriftState,
    ModelLiveLanes,
    ModelPlaceOutcome,
)


class InMemoryDriftOpenItems:
    def __init__(self, host: str, items: tuple[ModelDriftOpenItem, ...] = ()) -> None:
        self.host = host
        self.items = items

    def read(self) -> ModelDriftState:
        return ModelDriftState(host=self.host, items=self.items)


class InMemoryLiveLanes:
    def __init__(
        self,
        covered_causes: tuple[str, ...] = (),
        covered_lanes: tuple[str, ...] = (),
    ) -> None:
        self.covered_causes = covered_causes
        self.covered_lanes = covered_lanes
        self.asked: list[tuple[str, ...]] = []

    def read(self, cause_keys: tuple[str, ...]) -> ModelLiveLanes:
        self.asked.append(cause_keys)
        return ModelLiveLanes(
            covered_causes=self.covered_causes, covered_lanes=self.covered_lanes
        )


class InMemoryDispatchRecord:
    def __init__(self, record: dict[str, str] | None = None) -> None:
        self.record: dict[str, str] = dict(record or {})

    def load(self) -> dict[str, str]:
        return dict(self.record)

    def save(self, record: dict[str, str]) -> None:
        self.record = dict(record)


class InMemoryBriefWriter:
    def __init__(self) -> None:
        self.briefs: dict[str, str] = {}

    def write(self, name: str, text: str) -> str:
        self.briefs[name] = text
        return f"memory://{name}"


class InMemoryLanePlacer:
    """Records every argv; ``refuse`` makes the next placements fail with that detail."""

    def __init__(self) -> None:
        self.placed: list[tuple[str, ...]] = []
        self.refuse: str | None = None

    def place(self, argv: tuple[str, ...]) -> ModelPlaceOutcome:
        if self.refuse is not None:
            return ModelPlaceOutcome(started=False, detail=self.refuse)
        self.placed.append(argv)
        return ModelPlaceOutcome(started=True, detail="in-memory placement")


__all__ = [
    "InMemoryBriefWriter",
    "InMemoryDispatchRecord",
    "InMemoryDriftOpenItems",
    "InMemoryLanePlacer",
    "InMemoryLiveLanes",
]
