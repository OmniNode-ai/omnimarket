# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session phase state reducer node — OMN-11230."""

from omnimarket.nodes.node_session_phase_reducer.handlers.handler_session_phase_reducer import (
    HandlerSessionPhaseReducer,
)

__all__ = [
    "HandlerSessionPhaseReducer",
    "NodeSessionPhaseReducer",
]


class NodeSessionPhaseReducer(HandlerSessionPhaseReducer):
    """ONEX entry-point wrapper for HandlerSessionPhaseReducer."""
