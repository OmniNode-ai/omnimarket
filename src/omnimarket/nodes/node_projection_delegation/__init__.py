from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)

__all__ = [
    "DelegationProjectionRunner",
    "NodeProjectionDelegation",
]
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)


class NodeProjectionDelegation(HandlerProjectionDelegation):
    """ONEX entry-point wrapper for HandlerProjectionDelegation."""
