# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep each dev-lane rebuild the deploy agent completes, so a regression event can name the builds between two probe runs."""

from __future__ import annotations

from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelDevBuildRecorded,
    ModelRebuildCompletedObserved,
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProtocolDevBuildStore,
)


class HandlerDevBuildRecord:
    """Record one rebuild-completed event of the watched runtime lane; an event of another lane is not recorded."""

    def __init__(self, *, store: ProtocolDevBuildStore, runtime_lane: str) -> None:
        self._store = store
        self._lane = runtime_lane

    def handle(self, request: ModelRebuildCompletedObserved) -> ModelDevBuildRecorded:
        recorded = request.runtime_lane == self._lane
        if recorded:
            self._store.append(request)
        return ModelDevBuildRecorded(
            git_sha=request.git_sha,
            completed_at=request.completed_at,
            runtime_lane=request.runtime_lane,
            recorded=recorded,
        )
