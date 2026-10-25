# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local ONEX wrapper for node_lane_host_discovery_compute."""

from omnimarket.nodes.node_lane_host_discovery_compute.handlers.handler_lane_host_discovery import (
    HandlerLaneHostDiscovery,
)


class NodeLaneHostDiscoveryCompute(HandlerLaneHostDiscovery):
    """ONEX entry-point wrapper for HandlerLaneHostDiscovery."""


__all__ = ["HandlerLaneHostDiscovery", "NodeLaneHostDiscoveryCompute"]
