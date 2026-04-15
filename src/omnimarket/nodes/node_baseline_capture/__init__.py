"""node_baseline_capture — captures named system state snapshots."""

from omnimarket.nodes.node_baseline_capture.handlers.handler_baseline_capture import (
    HandlerBaselineCapture,
)


class NodeBaselineCapture(HandlerBaselineCapture):
    """ONEX entry-point wrapper for HandlerBaselineCapture."""
