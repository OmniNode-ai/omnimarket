"""node_dod_verify — DoD evidence verification compute node."""

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_completed_event import (
    ModelDodVerifyCompletedEvent,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)

__all__ = [
    "EnumDodVerifyStatus",
    "EnumEvidenceCheckStatus",
    "HandlerDodVerify",
    "ModelDodVerifyCompletedEvent",
    "ModelDodVerifyStartCommand",
    "ModelDodVerifyState",
    "ModelEvidenceCheckResult",
    "NodeDodVerify",
]


class NodeDodVerify(HandlerDodVerify):
    """ONEX entry-point wrapper for HandlerDodVerify."""
