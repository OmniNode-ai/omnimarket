# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_pilot_measurement_effect: where each pilot's measured result comes from."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_pilot_measurement_effect.models.model_pilot_measurement import (
    ModelPilotResultRead,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import (
    ModelPilotDeclaration,
)


class PilotResultError(RuntimeError):
    """The pilot's measured result could not be read (no file, or one that is not typed)."""


class ProtocolPilotResultSource(Protocol):
    """Read one pilot's measured result. A read that fails raises ``PilotResultError``."""

    def read(self, declaration: ModelPilotDeclaration) -> ModelPilotResultRead:
        """The pilot's metrics and when they were measured; raises ``PilotResultError``."""
        ...


__all__ = ["PilotResultError", "ProtocolPilotResultSource"]
