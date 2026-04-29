"""node_dispatch_queue_drainer — compile legacy dispatch queue items."""

from omnimarket.nodes.node_dispatch_queue_drainer.handlers import (
    HandlerDispatchQueueDrainer,
)
from omnimarket.nodes.node_dispatch_queue_drainer.models import (
    ModelDispatchQueueDrainerResult,
    ModelDispatchQueueItem,
)


class NodeDispatchQueueDrainer(HandlerDispatchQueueDrainer):
    """ONEX entry-point wrapper for HandlerDispatchQueueDrainer."""


__all__ = [
    "HandlerDispatchQueueDrainer",
    "ModelDispatchQueueDrainerResult",
    "ModelDispatchQueueItem",
    "NodeDispatchQueueDrainer",
]
