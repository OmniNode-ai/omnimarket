# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The named policy documents a lane overlay carries for a handler (OMN-21595)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProtocolOverlayDocument(Protocol):
    """Reads one named overlay document, or ``None`` when the overlay carries none."""

    def read(self, name: str) -> dict[str, Any] | None: ...


__all__: list[str] = ["ProtocolOverlayDocument"]
