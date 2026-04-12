"""node_pr_lifecycle_orchestrator — FSM orchestrator for pr_lifecycle domain."""

from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
)


class NodePrLifecycleOrchestrator(HandlerPrLifecycleOrchestrator):
    """ONEX entry-point wrapper for HandlerPrLifecycleOrchestrator."""
