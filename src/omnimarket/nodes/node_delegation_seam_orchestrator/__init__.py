# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_delegation_seam_orchestrator: the delegation seam line's read plan."""

from omnimarket.nodes.node_delegation_seam_orchestrator.handlers.handler_plan_seam_reads import (
    HandlerPlanSeamReads,
)

__all__ = ["HandlerPlanSeamReads", "NodeDelegationSeamOrchestrator"]


class NodeDelegationSeamOrchestrator(HandlerPlanSeamReads):
    """ONEX entry-point wrapper for HandlerPlanSeamReads."""
