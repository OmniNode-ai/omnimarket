# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_retrospective_calculate_compute: scaffold from the weekly_retrospective design."""

from omnimarket.nodes.node_retrospective_calculate_compute.handlers.handler_retrospective_calculate_compute import (
    HandlerRetrospectiveCalculateCompute,
)

__all__ = ["HandlerRetrospectiveCalculateCompute", "NodeRetrospectiveCalculateCompute"]


class NodeRetrospectiveCalculateCompute(HandlerRetrospectiveCalculateCompute):
    """ONEX entry-point wrapper for HandlerRetrospectiveCalculateCompute."""
