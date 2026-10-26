# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_landing_tick_bounds_compute — pure compute policy (OMN-17427)."""

from omnimarket.nodes.node_landing_tick_bounds_compute.handlers.handler_landing_tick_bounds import (
    HandlerLandingTickBounds,
)


class NodeLandingTickBoundsCompute(HandlerLandingTickBounds):
    """ONEX entry-point wrapper for HandlerLandingTickBounds."""


__all__ = ["HandlerLandingTickBounds", "NodeLandingTickBoundsCompute"]
