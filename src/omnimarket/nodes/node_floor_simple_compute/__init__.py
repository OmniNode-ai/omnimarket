# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_simple_compute: the simple floor and the lab-state verdicts, from readings (OMN-19998)."""

from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)

__all__ = ["HandlerFloorSimple", "NodeFloorSimpleCompute"]


class NodeFloorSimpleCompute(HandlerFloorSimple):
    """ONEX entry-point wrapper for HandlerFloorSimple."""
