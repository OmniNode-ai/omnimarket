# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam that persists the observer's cursors for node_github_schedule_observer_effect."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_github_schedule_observer_effect.models.model_schedule_observer_state import (
    ModelScheduleObserverState,
)


@runtime_checkable
class ProtocolScheduleObserverStateStore(Protocol):
    """Load and save the observer's persisted state."""

    def load(self) -> ModelScheduleObserverState:
        """Return the saved state, or an empty one when nothing was saved yet.

        Raises:
            ValueError: when saved state exists but cannot be read; the cursor
                is never silently reset.
        """
        ...

    def save(self, state: ModelScheduleObserverState) -> None:
        """Persist the state atomically."""
        ...
