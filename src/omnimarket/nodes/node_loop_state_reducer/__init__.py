# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build loop state reducer node — migrated from omnibase_infra [OMN-7577]."""

from omnimarket.nodes.node_loop_state_reducer.handlers.handler_loop_state import (
    HandlerLoopState,
)

__all__ = [
    "HandlerLoopState",
    "NodeLoopStateReducer",
]


class NodeLoopStateReducer(HandlerLoopState):
    """ONEX entry-point wrapper for HandlerLoopState."""
