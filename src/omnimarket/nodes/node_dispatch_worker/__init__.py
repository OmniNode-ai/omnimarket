"""node_dispatch_worker — compile worker dispatch spec into role-templated agent prompt."""

from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
    HandlerDispatchWorker,
)
from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command import (
    EnumWorkerRole,
    ModelDispatchWorkerCommand,
)
from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_result import (
    ModelDispatchWorkerResult,
)


class NodeDispatchWorker(HandlerDispatchWorker):
    """ONEX entry-point wrapper for HandlerDispatchWorker."""


__all__ = [
    "EnumWorkerRole",
    "HandlerDispatchWorker",
    "ModelDispatchWorkerCommand",
    "ModelDispatchWorkerResult",
    "NodeDispatchWorker",
]
