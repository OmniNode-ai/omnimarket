# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of node_overlay_drift_compute: where each declared file's text comes from."""

from __future__ import annotations

from typing import Protocol


class SourceReadError(RuntimeError):
    """A declared file could not be read or did not hold its value; the message names the cause, never content."""


class ProtocolFileSource(Protocol):
    """Read-only view of the declared files. A read that fails raises ``SourceReadError``."""

    def text(self, path: str) -> str:
        """The text of the file declared at ``path`` (the overlay's own spelling of it)."""
        ...


__all__ = ["ProtocolFileSource", "SourceReadError"]
