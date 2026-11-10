# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_simple_scheduled_effect: the simple floor computed on a declared schedule in the runtime (OMN-23072)."""

from omnimarket.nodes.node_floor_simple_scheduled_effect.handlers.handler_floor_simple_scheduled import (
    HandlerFloorSimpleScheduled,
)

__all__ = ["HandlerFloorSimpleScheduled", "NodeFloorSimpleScheduledEffect"]


class NodeFloorSimpleScheduledEffect(HandlerFloorSimpleScheduled):
    """ONEX entry-point wrapper for HandlerFloorSimpleScheduled."""
