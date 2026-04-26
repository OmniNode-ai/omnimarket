"""node_dispatch_worker_execution_effect — Execute compiled worker specs."""

from omnimarket.nodes.node_dispatch_worker_execution_effect.handlers.handler_dispatch_worker_execution import (
    HandlerDispatchWorkerExecution,
)
from omnimarket.nodes.node_dispatch_worker_execution_effect.models import (
    EnumDispatchWorkerExecutionStatus,
    ModelCompiledDispatchWorker,
    ModelDispatchWorkerDelegationPayload,
    ModelDispatchWorkerExecutionInput,
    ModelDispatchWorkerExecutionOutcome,
    ModelDispatchWorkerExecutionResult,
    ModelDispatchWorkerSpecArtifact,
)


class NodeDispatchWorkerExecutionEffect(HandlerDispatchWorkerExecution):
    """ONEX entry-point wrapper for HandlerDispatchWorkerExecution."""


__all__ = [
    "EnumDispatchWorkerExecutionStatus",
    "HandlerDispatchWorkerExecution",
    "ModelCompiledDispatchWorker",
    "ModelDispatchWorkerDelegationPayload",
    "ModelDispatchWorkerExecutionInput",
    "ModelDispatchWorkerExecutionOutcome",
    "ModelDispatchWorkerExecutionResult",
    "ModelDispatchWorkerSpecArtifact",
    "NodeDispatchWorkerExecutionEffect",
]
