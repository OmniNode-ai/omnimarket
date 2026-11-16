# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step's source error (OMN-23409)."""

from __future__ import annotations


class SnapshotSourceError(Exception):
    """A declared source could not be read; the tick ends in the typed failed event."""


class ControllerTickOpenError(SnapshotSourceError):
    """The controller's facts file is a tick its tick log has not finished: its decision is not yet known (OMN-23665)."""


class ControllerStateStaleError(SnapshotSourceError):
    """The controller's last finished tick is older than the overlay's bound (OMN-23665)."""


__all__: list[str] = [
    "ControllerStateStaleError",
    "ControllerTickOpenError",
    "SnapshotSourceError",
]
