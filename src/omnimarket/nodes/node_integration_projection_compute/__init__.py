# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_integration_projection_compute: the newest integration reading per merged change, with counts (OMN-23899)."""

from omnimarket.nodes.node_integration_projection_compute.handlers import (
    HandlerIntegrationProjection,
)

__all__ = ["HandlerIntegrationProjection", "NodeIntegrationProjectionCompute"]


class NodeIntegrationProjectionCompute(HandlerIntegrationProjection):
    """ONEX entry-point wrapper for HandlerIntegrationProjection."""
