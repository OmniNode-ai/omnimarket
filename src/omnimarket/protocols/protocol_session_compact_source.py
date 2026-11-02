# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reader port for a session's captured compact content and published activations."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.context_install_confirm_trigger import (
    ModelSessionCompactSnapshot,
)


class ProtocolSessionCompactSource(Protocol):
    """Read the compact content and activations of the session a content event belongs to."""

    def read(self, event_id: str) -> ModelSessionCompactSnapshot | None:
        """Return the snapshot of the event's session, or None when the event is unknown."""
        ...


__all__ = ["ProtocolSessionCompactSource"]
