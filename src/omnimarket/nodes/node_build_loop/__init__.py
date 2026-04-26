"""node_build_loop — Autonomous build loop WorkflowPackage with 6-phase FSM."""

from omnimarket.nodes.node_build_loop.handlers.handler_build_loop import (
    HandlerBuildLoop,
)
from omnimarket.nodes.node_build_loop.models.model_loop_completed_event import (
    ModelLoopCompletedEvent,
)
from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
    ModelLoopStartCommand,
)
from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    TERMINAL_PHASES,
    EnumBuildLoopPhase,
    ModelLoopState,
)
from omnimarket.nodes.node_build_loop.models.model_phase_transition_event import (
    ModelPhaseTransitionEvent,
)

__all__ = [
    "TERMINAL_PHASES",
    "EnumBuildLoopPhase",
    "HandlerBuildLoop",
    "ModelLoopCompletedEvent",
    "ModelLoopStartCommand",
    "ModelLoopState",
    "ModelPhaseTransitionEvent",
    "NodeBuildLoop",
]


class NodeBuildLoop(HandlerBuildLoop):
    """ONEX entry-point wrapper for HandlerBuildLoop."""
