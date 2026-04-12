"""node_baseline_compare — diffs current state against a baseline artifact."""

from omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare import (
    HandlerBaselineCompare,
)


class NodeBaselineCompare(HandlerBaselineCompare):
    """ONEX entry-point wrapper for HandlerBaselineCompare."""
