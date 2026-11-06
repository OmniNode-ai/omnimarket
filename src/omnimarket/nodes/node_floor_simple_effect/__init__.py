# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_simple_effect: one 17a cycle, reads in and the simple floor out (OMN-19998)."""

from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)

__all__ = ["HandlerFloorSimpleTick", "NodeFloorSimpleEffect"]


class NodeFloorSimpleEffect(HandlerFloorSimpleTick):
    """ONEX entry-point wrapper for HandlerFloorSimpleTick."""
