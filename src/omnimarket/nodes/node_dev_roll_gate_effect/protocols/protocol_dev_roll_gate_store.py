# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The durable store seam of node_dev_roll_gate_effect."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_dev_roll_gate_effect.models.model_dev_roll_gate_state import (
    ModelDevRollGateState,
)


@runtime_checkable
class ProtocolDevRollGateStore(Protocol):
    """Load and save the gate's whole state."""

    def load(self) -> ModelDevRollGateState:
        """Return the stored state; an empty state when none was stored."""
        ...

    def save(self, state: ModelDevRollGateState) -> None:
        """Replace the stored state; the write is durable when this returns."""
        ...
