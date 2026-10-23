# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane queue decisions that respect each lane's usable slots and claimants."""

from omnimarket.nodes.node_lane_queue_start_decision_compute.handlers.handler_lane_queue_start_decision import (
    HandlerLaneQueueStartDecision,
)


class NodeLaneQueueStartDecisionCompute(HandlerLaneQueueStartDecision):
    """ONEX entrypoint for pure lane queue start decisions."""


__all__ = ["HandlerLaneQueueStartDecision", "NodeLaneQueueStartDecisionCompute"]
