# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_landing_upstream_gate_compute — pure compute policy (OMN-19998)."""

from omnimarket.nodes.node_landing_upstream_gate_compute.handlers.handler_landing_upstream_gate import (
    HandlerLandingUpstreamGate,
)


class NodeLandingUpstreamGateCompute(HandlerLandingUpstreamGate):
    """ONEX entry-point wrapper for HandlerLandingUpstreamGate."""


__all__ = ["HandlerLandingUpstreamGate", "NodeLandingUpstreamGateCompute"]
