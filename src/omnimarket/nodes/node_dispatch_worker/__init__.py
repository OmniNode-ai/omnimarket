"""node_dispatch_worker — compile worker dispatch spec into role-templated agent prompt."""

from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
    HandlerDispatchWorker,
)


class NodeDispatchWorker(HandlerDispatchWorker):
    """ONEX entry-point wrapper for HandlerDispatchWorker."""
