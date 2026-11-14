# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate's durable store, behind one protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollGateState,
)


@runtime_checkable
class ProtocolDevRollGateStore(Protocol):
    """Load and save the node's whole gate state."""

    def load(self) -> ModelDevRollGateState:
        """Return the stored state; an empty state when none was stored."""
        ...

    def save(self, state: ModelDevRollGateState) -> None:
        """Replace the stored state."""
        ...
