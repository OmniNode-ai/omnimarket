"""node_ci_watch — CI polling, failure classification, and auto-fix loop."""

from omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch import (
    EnumCiTerminalStatus,
    HandlerCiWatch,
    ModelCiFixCycle,
    ModelCiWatchCommand,
    ModelCiWatchResult,
    ModelFailedCheck,
)

__all__ = [
    "EnumCiTerminalStatus",
    "HandlerCiWatch",
    "ModelCiFixCycle",
    "ModelCiWatchCommand",
    "ModelCiWatchResult",
    "ModelFailedCheck",
    "NodeCiWatch",
]


class NodeCiWatch(HandlerCiWatch):
    """ONEX entry-point wrapper for HandlerCiWatch."""
