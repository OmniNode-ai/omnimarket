# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_delegation_readiness_verdict_compute: the shippable verdict over the delegation readiness scenarios (OMN-23181)."""

from omnimarket.nodes.node_delegation_readiness_verdict_compute.handlers.handler_readiness_verdict import (
    HandlerReadinessVerdict,
)

__all__ = ["HandlerReadinessVerdict", "NodeDelegationReadinessVerdictCompute"]


class NodeDelegationReadinessVerdictCompute(HandlerReadinessVerdict):
    """ONEX entry-point wrapper for HandlerReadinessVerdict."""
