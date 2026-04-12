"""node_design_to_plan — End-to-end design workflow to structured implementation plans."""

from omnimarket.nodes.node_design_to_plan.handlers.handler_design_to_plan import (
    HandlerDesignToPlan,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
    ModelDesignToPlanCompletedEvent,
    ModelDesignToPlanStartCommand,
)

__all__ = [
    "HandlerDesignToPlan",
    "ModelDesignToPlanCompletedEvent",
    "ModelDesignToPlanStartCommand",
    "NodeDesignToPlan",
]


class NodeDesignToPlan(HandlerDesignToPlan):
    """ONEX entry-point wrapper for HandlerDesignToPlan."""
