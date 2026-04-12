"""node_data_verification — post-pipeline data verification compute node."""

from omnimarket.nodes.node_data_verification.handlers.handler_data_verification import (
    DataSource,
    HandlerDataVerification,
    InmemoryDataSource,
)
from omnimarket.nodes.node_data_verification.models.model_data_verification_completed_event import (
    ModelDataVerificationCompletedEvent,
)
from omnimarket.nodes.node_data_verification.models.model_data_verification_start_command import (
    ModelDataVerificationStartCommand,
)
from omnimarket.nodes.node_data_verification.models.model_data_verification_state import (
    EnumDataCheck,
    EnumVerificationStatus,
    ModelDataVerificationResult,
    ModelSampleRow,
)

__all__ = [
    "NodeDataVerification",
    "DataSource",
    "EnumDataCheck",
    "EnumVerificationStatus",
    "HandlerDataVerification",
    "InmemoryDataSource",
    "ModelDataVerificationCompletedEvent",
    "ModelDataVerificationResult",
    "ModelDataVerificationStartCommand",
    "ModelSampleRow",
]

class NodeDataVerification(HandlerDataVerification):
    """ONEX entry-point wrapper for HandlerDataVerification."""
