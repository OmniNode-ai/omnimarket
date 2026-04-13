# SPDX-License-Identifier: MIT
"""node_handoff_effect -- Captures session git state and writes a handoff artifact."""

from omnimarket.nodes.node_handoff_effect.handlers.handler_handoff_effect import (
    HandlerHandoffEffect,
)

__all__ = ["HandlerHandoffEffect", "NodeHandoffEffect"]


class NodeHandoffEffect(HandlerHandoffEffect):
    """ONEX entry-point wrapper for HandlerHandoffEffect."""
