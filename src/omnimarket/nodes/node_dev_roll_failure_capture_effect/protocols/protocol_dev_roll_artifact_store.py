# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The artifact store seam of node_dev_roll_failure_capture_effect."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolDevRollArtifactStore(Protocol):
    """Write and read artifacts by key."""

    def write(self, key: str, content: bytes) -> str:
        """Store the content; return the sha256 hex of what was stored."""
        ...

    def read(self, key: str) -> bytes | None:
        """Return the stored bytes, or None when the key is absent."""
        ...
