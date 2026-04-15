# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR lifecycle state reducer node — pure FSM reducer [OMN-8086]."""

from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    HandlerPrLifecycleStateReducer,
)

__all__ = [
    "HandlerPrLifecycleStateReducer",
    "NodePrLifecycleStateReducer",
]


class NodePrLifecycleStateReducer(HandlerPrLifecycleStateReducer):
    """ONEX entry-point wrapper for HandlerPrLifecycleStateReducer."""
