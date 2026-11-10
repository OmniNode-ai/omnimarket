# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_page_api_effect: the runtime route for the floor's JSON (OMN-23072)."""

from omnimarket.nodes.node_floor_page_api_effect.handlers.handler_floor_page import (
    HandlerFloorPage,
)

__all__ = ["HandlerFloorPage", "NodeFloorPageApiEffect"]


class NodeFloorPageApiEffect(HandlerFloorPage):
    """ONEX entry-point wrapper for HandlerFloorPage."""
