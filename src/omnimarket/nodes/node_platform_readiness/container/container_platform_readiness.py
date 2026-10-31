# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    NodePlatformReadiness,
)


def build_node_platform_readiness() -> NodePlatformReadiness:
    """Build the handler with its constructor defaults."""
    return NodePlatformReadiness()
