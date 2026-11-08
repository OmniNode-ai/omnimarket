# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the order writer reads that a piece is ready for the order to move past it."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolOrderReadiness(Protocol):
    """A read of one piece's readiness; it never writes."""

    def is_ready(self, piece: str) -> bool:
        """True when the order may advance past this piece."""
        ...
