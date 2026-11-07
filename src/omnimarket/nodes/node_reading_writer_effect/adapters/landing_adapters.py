# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where piece 9's landing snapshot comes from (OMN-22918).

``WatcherFileLandingSource`` reads the pull request watcher's state file and the landing goal's scope
overlay, and counts what the goal counts through the same reader the floor uses, so the reading and the
floor's numbers cannot drift apart. A state file or scope that cannot be read is an error, never an empty
queue: an unread scope puts nothing in scope, and an empty queue would read WORKING. ``InMemoryLandingSource``
answers a fixed snapshot or error and is the default of the tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.floor_17a.model_floor_17a import ModelGoalScope
from omnimarket.models.model_reading import (
    ModelLandingHead,
    ModelLandingRequirements,
    ModelLandingSnapshot,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import read_watcher
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLandingRoute
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLandingRead


class InMemoryLandingSource:
    """A fixed answer: the in-memory backend of the landing source."""

    def __init__(
        self,
        snapshot: ModelLandingSnapshot | None = None,
        *,
        error: str | None = None,
    ) -> None:
        if (snapshot is None) == (error is None):
            raise ValueError("an in-memory landing source holds a snapshot or an error")
        self._answer = ModelLandingRead(snapshot=snapshot, error=error)

    def read(
        self, requirements: ModelLandingRequirements, now: datetime
    ) -> ModelLandingRead:
        return self._answer


class WatcherFileLandingSource:
    """The real backend: the watcher's state file and the goal scope overlay the route names."""

    def __init__(self, route: ModelLandingRoute, environ: Mapping[str, str]) -> None:
        self._route = route
        self._environ = environ

    def read(
        self, requirements: ModelLandingRequirements, now: datetime
    ) -> ModelLandingRead:
        path_text = self._environ.get(self._route.watcher_state_env, "").strip()
        if not path_text:
            return ModelLandingRead(error=f"{self._route.watcher_state_env} is not set")
        try:
            scope = load_structured_overlay(
                self._route.goal_scope_overlay_id,
                ModelGoalScope,
                self._environ,
                file_env=self._route.goal_scope_file_env,
            )
        except NodeOverlayError as exc:
            return ModelLandingRead(
                error=f"the goal scope overlay is not read ({type(exc).__name__})"
            )
        now = now.astimezone(UTC)
        watcher = read_watcher(
            Path(path_text),
            now,
            excluded_classes=(),
            queue_classes=self._route.queue_classes,
            merge_window_minutes=requirements.merge_window_seconds // 60,
            scope=scope,
            conflict_states=self._route.conflict_merge_states,
        )
        if watcher is None:
            return ModelLandingRead(error="the watcher state file is not read")
        if not watcher.scoped or watcher.merges_in_window is None:
            return ModelLandingRead(error="the landing goal scope is not read")
        heads = tuple(
            ModelLandingHead(
                ref=ref,
                waiting_seconds=minutes * 60,
                conflicted=ref in watcher.queue_conflicted,
            )
            for ref, minutes in watcher.queue_ages
        )
        return ModelLandingRead(
            snapshot=ModelLandingSnapshot(
                age_seconds=max(int((now - watcher.last_tick).total_seconds()), 0),
                heads=heads,
                merges_in_window=watcher.merges_in_window,
            )
        )


__all__ = ["InMemoryLandingSource", "WatcherFileLandingSource"]
