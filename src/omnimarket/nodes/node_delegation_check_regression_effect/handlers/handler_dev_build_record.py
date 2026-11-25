# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep each dev-lane rebuild the deploy agent completes, so a regression event can name the builds between two probe runs."""

from __future__ import annotations

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.state_files import (
    FileDevBuildStore,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelDevBuildRecorded,
    ModelRebuildCompletedObserved,
)
from omnimarket.nodes.node_delegation_check_regression_effect.overlay import (
    load_settings,
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProtocolDevBuildStore,
)


class HandlerDevBuildRecord:
    """Record one rebuild-completed event of the watched runtime lane; an event of another lane is not recorded.

    The runtime builds it with no argument and the store and the lane are made from the node's overlay on each request; a
    caller that passes both gets exactly those, and one of the two is refused."""

    def __init__(
        self,
        *,
        store: ProtocolDevBuildStore | None = None,
        runtime_lane: str | None = None,
    ) -> None:
        if (store is None) != (runtime_lane is None):
            raise ValueError("pass both the store and the runtime lane, or neither")
        self._store = store
        self._lane = runtime_lane

    def handle(self, request: ModelRebuildCompletedObserved) -> ModelDevBuildRecorded:
        store, lane = self._store, self._lane
        if store is None or lane is None:
            settings = load_settings()
            if settings.registry_root is None:
                raise NodeOverlayError(
                    "the overlay names no registry_root, so the build has nowhere to be kept"
                )
            store = FileDevBuildStore(
                settings.registry_root / settings.dev_builds_relpath
            )
            lane = settings.dev_runtime_lane
        recorded = request.runtime_lane == lane
        if recorded:
            store.append(request)
        return ModelDevBuildRecorded(
            git_sha=request.git_sha,
            completed_at=request.completed_at,
            runtime_lane=request.runtime_lane,
            recorded=recorded,
        )
