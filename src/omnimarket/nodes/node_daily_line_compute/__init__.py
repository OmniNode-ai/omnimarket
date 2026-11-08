# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_daily_line_compute: the daily line of rebuild plan section 11, computed from three readings (OMN-22925)."""

from omnimarket.nodes.node_daily_line_compute.handlers.handler_daily_line import (
    HandlerDailyLine,
)

__all__ = ["HandlerDailyLine", "NodeDailyLineCompute"]


class NodeDailyLineCompute(HandlerDailyLine):
    """ONEX entry-point wrapper for HandlerDailyLine."""
