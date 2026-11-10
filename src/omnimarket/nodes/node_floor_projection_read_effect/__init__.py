# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's projection reader: the bus projections the floor takes its inputs from, each with its own time."""

from omnimarket.nodes.node_floor_projection_read_effect.handlers import (
    HandlerFloorProjectionRead,
)


class NodeFloorProjectionReadEffect(HandlerFloorProjectionRead):
    """ONEX entry point."""


__all__ = ["HandlerFloorProjectionRead", "NodeFloorProjectionReadEffect"]
