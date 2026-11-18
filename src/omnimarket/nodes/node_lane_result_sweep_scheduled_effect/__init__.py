# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lane_result_sweep_scheduled_effect: the lane result sweep on the runtime tick (OMN-24262)."""

from omnimarket.nodes.node_lane_result_sweep_scheduled_effect.handlers.handler_lane_result_sweep_scheduled import (
    HandlerLaneResultSweepScheduled,
)

__all__ = ["HandlerLaneResultSweepScheduled", "NodeLaneResultSweepScheduledEffect"]


class NodeLaneResultSweepScheduledEffect(HandlerLaneResultSweepScheduled):
    """ONEX entry-point wrapper for HandlerLaneResultSweepScheduled."""
