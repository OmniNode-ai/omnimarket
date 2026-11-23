# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool snapshot step's source errors (OMN-23664)."""

from __future__ import annotations

from omnimarket.models.model_lane_host_pool_unreadable import (
    LaneHostPoolUnreadableError,
)


class PoolSnapshotSourceError(RuntimeError):
    """A declared source could not be read; the tick ends in the typed failed event."""


class PoolSnapshotStaleError(PoolSnapshotSourceError, LaneHostPoolUnreadableError):
    """The snapshot record is missing, unreadable, or older than the freshness bound; the lane start ends it pool-unreadable.

    ``age_seconds`` is the record's age when it was read and stale; ``None`` when there was no age to state. ``absent`` is True when
    there was no record file at all. ``bound_seconds`` is the freshness bound the reader held it to, ``None`` when the error did
    not come from the reader of a bound.
    """

    def __init__(
        self,
        message: str,
        *,
        age_seconds: int | None = None,
        bound_seconds: int | None = None,
        absent: bool = False,
    ) -> None:
        super().__init__(message)
        self.age_seconds = age_seconds
        self.bound_seconds = bound_seconds
        self.absent = absent


__all__: list[str] = ["PoolSnapshotSourceError", "PoolSnapshotStaleError"]
