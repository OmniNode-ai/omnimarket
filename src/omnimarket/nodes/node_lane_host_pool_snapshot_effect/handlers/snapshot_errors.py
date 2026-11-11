# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool snapshot step's source errors (OMN-23664)."""

from __future__ import annotations


class PoolSnapshotSourceError(RuntimeError):
    """A declared source could not be read; the tick ends in the typed failed event."""


class PoolSnapshotStaleError(PoolSnapshotSourceError):
    """The snapshot record is missing, unreadable, or older than the freshness bound."""


__all__: list[str] = ["PoolSnapshotSourceError", "PoolSnapshotStaleError"]
