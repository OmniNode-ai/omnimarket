# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_consumer_flow — throughput truth as a projection (OMN-16777)."""

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_projection_consumer_flow import (
    HandlerProjectionConsumerFlow,
)

__all__ = [
    "HandlerProjectionConsumerFlow",
    "NodeProjectionConsumerFlow",
]


class NodeProjectionConsumerFlow(HandlerProjectionConsumerFlow):
    """ONEX entry-point wrapper for HandlerProjectionConsumerFlow."""
