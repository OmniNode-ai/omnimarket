# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the overlay text lives: injected, so the default needs no disk (OMN-20330)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ProtocolOverlayFileStore(Protocol):
    """Read and replace one overlay file."""

    def read_text(self, path: Path) -> str | None:
        """The file's text, or ``None`` when there is none."""
        ...

    def write_text(self, path: Path, text: str) -> None:
        """Replace the file with ``text``, all or nothing."""
        ...


__all__ = ["ProtocolOverlayFileStore"]
