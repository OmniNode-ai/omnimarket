# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The observation store seam of node_dev_roll_decision_request_effect."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_dev_roll_decision_request_effect.models.model_dev_roll_observation_state import (
    ModelDevRollObservationState,
)


@runtime_checkable
class ProtocolDevRollObservationStore(Protocol):
    """Load and save the node's whole observation state."""

    def load(self) -> ModelDevRollObservationState:
        """Return the stored state; an empty state when none was stored."""
        ...

    def save(self, state: ModelDevRollObservationState) -> None:
        """Replace the stored state."""
        ...
