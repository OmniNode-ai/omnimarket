"""node_close_out — Close-out pipeline WorkflowPackage with multi-phase FSM."""

from omnimarket.nodes.node_close_out.handlers.handler_close_out import (
    HandlerCloseOut,
)
from omnimarket.nodes.node_close_out.models.model_close_out_completed_event import (
    ModelCloseOutCompletedEvent,
)
from omnimarket.nodes.node_close_out.models.model_close_out_phase_event import (
    ModelCloseOutPhaseEvent,
)
from omnimarket.nodes.node_close_out.models.model_close_out_start_command import (
    ModelCloseOutStartCommand,
)
from omnimarket.nodes.node_close_out.models.model_close_out_state import (
    EnumCloseOutPhase,
    ModelCloseOutState,
)

__all__ = [
    "EnumCloseOutPhase",
    "HandlerCloseOut",
    "ModelCloseOutCompletedEvent",
    "ModelCloseOutPhaseEvent",
    "ModelCloseOutStartCommand",
    "ModelCloseOutState",
    "NodeCloseOut",
]


class NodeCloseOut(HandlerCloseOut):
    """ONEX entry-point wrapper for HandlerCloseOut."""
