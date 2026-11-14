# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory backend, the default: fixed items and live lanes, a dict for the record, recorders for briefs and the runner."""

from __future__ import annotations

import hashlib

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelBriefPrepared,
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


class InMemoryBriefPreparer:
    """Records every brief and stamps the sha256 of the ones it passes, as the lane-brief check does.

    ``refuse`` makes the next briefs fail the check with that detail and leaves them unstamped.
    """

    def __init__(self) -> None:
        self.briefs: dict[str, str] = {}
        self.stamped: set[str] = set()
        self.refuse: str | None = None

    def prepare(self, *, lane: str, name: str, text: str) -> ModelBriefPrepared:
        self.briefs[name] = text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self.refuse is not None:
            return ModelBriefPrepared(
                path=f"memory://{name}", passed=False, sha256=digest, detail=self.refuse
            )
        self.stamped.add(digest)
        return ModelBriefPrepared(
            path=f"memory://{name}", passed=True, sha256=digest, detail=""
        )


class InMemoryLanePlacer:
    """Records every argv; ``refuse`` makes the next placements fail with that detail.

    Given the preparer, it refuses a brief whose sha256 the preparer never stamped, as the remote-lane runner
    does before any placement (``REMOTE-LANE-BRIEF-UNCHECKED``).
    """

    def __init__(self, briefs: InMemoryBriefPreparer | None = None) -> None:
        self.placed: list[tuple[str, ...]] = []
        self.refuse: str | None = None
        self._briefs = briefs

    def place(self, argv: tuple[str, ...]) -> ModelPlaceOutcome:
        if self.refuse is not None:
            return ModelPlaceOutcome(started=False, detail=self.refuse)
        if self._briefs is not None:
            name = argv[argv.index("--brief") + 1].removeprefix("memory://")
            text = self._briefs.briefs.get(name)
            digest = (
                hashlib.sha256(text.encode("utf-8")).hexdigest()
                if text is not None
                else ""
            )
            if digest not in self._briefs.stamped:
                return ModelPlaceOutcome(
                    started=False,
                    detail="REMOTE-LANE-BRIEF-UNCHECKED: no passing lane-brief check for this brief's current content",
                )
        self.placed.append(argv)
        return ModelPlaceOutcome(started=True, detail="in-memory placement")


__all__ = [
    "InMemoryBriefPreparer",
    "InMemoryDispatchRecord",
    "InMemoryDriftOpenItems",
    "InMemoryLanePlacer",
    "InMemoryLiveLanes",
]
