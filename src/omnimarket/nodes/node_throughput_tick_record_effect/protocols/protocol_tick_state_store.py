# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick's state directory as the record step sees it (OMN-23734)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolTickStateStore(Protocol):
    """Where the tick's rows are kept. Every write raises ``OSError`` when it cannot be made."""

    def path(self, name: str) -> str:
        """The full path a row named ``name`` lives at (for the error text)."""
        ...

    def write_atomic(self, name: str, text: str) -> None:
        """Replace the row whole, so a reader never sees half of it."""
        ...

    def append_text(self, name: str, text: str) -> None:
        """Append to the row's log."""
        ...


__all__ = ["ProtocolTickStateStore"]
