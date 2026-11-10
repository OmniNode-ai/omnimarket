# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_retrospective_run_orchestrator: scaffold from the weekly_retrospective design."""

from omnimarket.nodes.node_retrospective_run_orchestrator.handlers.handler_retrospective_run_orchestrator import (
    HandlerRetrospectiveRunOrchestrator,
)

__all__ = ["HandlerRetrospectiveRunOrchestrator", "NodeRetrospectiveRunOrchestrator"]


class NodeRetrospectiveRunOrchestrator(HandlerRetrospectiveRunOrchestrator):
    """ONEX entry-point wrapper for HandlerRetrospectiveRunOrchestrator."""
