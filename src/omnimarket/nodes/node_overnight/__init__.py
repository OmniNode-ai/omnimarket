"""node_overnight — Overnight session orchestrator WorkflowPackage."""

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumOvernightStatus,
    EnumPhase,
    HandlerOvernight,
    ModelOvernightCommand,
    ModelOvernightResult,
    ModelPhaseResult,
)

__all__ = [
    "EnumOvernightStatus",
    "EnumPhase",
    "HandlerOvernight",
    "ModelOvernightCommand",
    "ModelOvernightResult",
    "ModelPhaseResult",
    "NodeOvernight",
]
from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    HandlerBuildLoopExecutor,
)


class NodeOvernight(HandlerBuildLoopExecutor):
    """ONEX entry-point wrapper for HandlerBuildLoopExecutor."""
