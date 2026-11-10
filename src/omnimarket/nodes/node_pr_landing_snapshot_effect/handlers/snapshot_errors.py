# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step's source error (OMN-23409)."""

from __future__ import annotations


class SnapshotSourceError(Exception):
    """A declared source could not be read; the tick ends in the typed failed event."""


__all__: list[str] = ["SnapshotSourceError"]
