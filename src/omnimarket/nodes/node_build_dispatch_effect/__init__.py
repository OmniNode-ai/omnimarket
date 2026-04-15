"""node_build_dispatch_effect — Dispatches ticket-pipeline builds via delegation."""

from omnimarket.nodes.node_build_dispatch_effect.handlers.handler_build_dispatch import (
    HandlerBuildDispatch,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_build_dispatch_input import (
    ModelBuildDispatchInput,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_build_dispatch_outcome import (
    ModelBuildDispatchOutcome,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_build_dispatch_result import (
    ModelBuildDispatchResult,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_build_target import (
    EnumBuildability,
    ModelBuildTarget,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_delegation_payload import (
    ModelDelegationPayload,
)

__all__ = [
    "EnumBuildability",
    "HandlerBuildDispatch",
    "ModelBuildDispatchInput",
    "ModelBuildDispatchOutcome",
    "ModelBuildDispatchResult",
    "ModelBuildTarget",
    "ModelDelegationPayload",
    "NodeBuildDispatchEffect",
]


class NodeBuildDispatchEffect(HandlerBuildDispatch):
    """ONEX entry-point wrapper for HandlerBuildDispatch."""
