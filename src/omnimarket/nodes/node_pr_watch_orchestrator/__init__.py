"""Native PR watch orchestrator node."""

from omnimarket.nodes.node_pr_watch_orchestrator.handlers.handler_pr_watch_orchestrator import (
    HandlerPrWatchOrchestrator,
)

__all__ = ["HandlerPrWatchOrchestrator", "NodePrWatchOrchestrator"]


class NodePrWatchOrchestrator(HandlerPrWatchOrchestrator):
    """ONEX entry-point wrapper for HandlerPrWatchOrchestrator."""
