# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool snapshot step's source errors (OMN-23664)."""

from __future__ import annotations

from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    LaneHostPoolUnreadableError,
)


class PoolSnapshotSourceError(RuntimeError):
    """A declared source could not be read; the tick ends in the typed failed event."""


class PoolSnapshotStaleError(PoolSnapshotSourceError, LaneHostPoolUnreadableError):
    """The snapshot record is missing, unreadable, or older than the freshness bound; the lane start ends it pool-unreadable."""


__all__: list[str] = ["PoolSnapshotSourceError", "PoolSnapshotStaleError"]
