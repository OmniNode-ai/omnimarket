# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_session_phase_dispatcher — session phase transition event publisher."""

from omnimarket.nodes.node_session_phase_dispatcher.handlers.handler_session_phase_dispatcher import (
    HandlerSessionPhaseDispatcher,
)

__all__ = [
    "HandlerSessionPhaseDispatcher",
    "NodeSessionPhaseDispatcher",
]


class NodeSessionPhaseDispatcher(HandlerSessionPhaseDispatcher):
    """ONEX entry-point wrapper for HandlerSessionPhaseDispatcher."""
