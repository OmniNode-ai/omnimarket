# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Trusted routing complexity compute node (OMN-18341)."""

from omnimarket.nodes.node_routing_complexity_compute.handlers.handler_routing_complexity import (
    HandlerRoutingComplexity,
)


class NodeRoutingComplexityCompute(HandlerRoutingComplexity):
    """ONEX entrypoint for the deterministic trusted-input routing compute."""


__all__ = ["HandlerRoutingComplexity", "NodeRoutingComplexityCompute"]
