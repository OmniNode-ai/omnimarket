"""node_closeout_effect -- Close-out effect node with protocol-based DI.

Executes close-out phase: merge-sweep, quality gates, release readiness.
Dependencies injected via ProtocolMergeSweeper and ProtocolQualityGateChecker.
"""

from omnimarket.nodes.node_closeout_effect.handlers.handler_closeout import (
    HandlerCloseout,
)
from omnimarket.nodes.node_closeout_effect.models.model_closeout_input import (
    ModelCloseoutInput,
)
from omnimarket.nodes.node_closeout_effect.models.model_closeout_result import (
    ModelCloseoutResult,
)
from omnimarket.nodes.node_closeout_effect.protocols import (
    ProtocolMergeSweeper,
    ProtocolQualityGateChecker,
)

__all__ = [
    "HandlerCloseout",
    "ModelCloseoutInput",
    "ModelCloseoutResult",
    "NodeCloseoutEffect",
    "ProtocolMergeSweeper",
    "ProtocolQualityGateChecker",
]


class NodeCloseoutEffect(HandlerCloseout):
    """ONEX entry-point wrapper for HandlerCloseout."""
