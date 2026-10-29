# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_delegation_seam_measure_compute: the delegation workflow registry and seam readers."""

from omnimarket.nodes.node_delegation_seam_measure_compute.handlers.handler_load_delegation_workflows import (
    HandlerLoadDelegationWorkflows,
)

__all__ = ["HandlerLoadDelegationWorkflows", "NodeDelegationSeamMeasureCompute"]


class NodeDelegationSeamMeasureCompute(HandlerLoadDelegationWorkflows):
    """ONEX entry-point wrapper for HandlerLoadDelegationWorkflows."""
