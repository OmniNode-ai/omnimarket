# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Artifact store seam of node_dev_roll_failure_capture_effect (OMN-24013).

The handlers write the roll's captured log and terminal record through this
protocol. The in-memory adapter is deterministic; the filesystem adapter prunes
by retention and refuses names that escape its root.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class RollArtifactStoreError(Exception):
    """The store could not write the artifact."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@runtime_checkable
class ProtocolRollArtifactStore(Protocol):
    """Write one artifact and return its store reference."""

    def write(self, name: str, content: bytes) -> str:
        """Return the store reference; raise :class:`RollArtifactStoreError` when it cannot write."""
        ...
