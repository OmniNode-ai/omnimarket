"""Node entry point for savings projection."""

from __future__ import annotations

from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    HandlerProjectionSavings,
)


class NodeProjectionSavings(HandlerProjectionSavings):
    """ONEX entry-point wrapper for HandlerProjectionSavings."""


__all__ = ["NodeProjectionSavings"]
