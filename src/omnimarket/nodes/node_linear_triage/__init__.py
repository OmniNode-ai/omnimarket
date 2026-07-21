from omnimarket.nodes.node_linear_triage.handlers.handler_completion_reconcile import (
    HandlerCompletionReconcile,
)
from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    HandlerLinearTriage,
)


class NodeLinearTriage(HandlerLinearTriage):
    """ONEX entry-point wrapper for HandlerLinearTriage."""


# OMN-14915: the reverse-path completion reconciler is exposed on the node's
# public surface so the runtime/dispatch layer and reconciliation callers reach
# it as part of this node (it reuses this node's own close_evidence_gate).
__all__ = ["HandlerCompletionReconcile", "NodeLinearTriage"]
