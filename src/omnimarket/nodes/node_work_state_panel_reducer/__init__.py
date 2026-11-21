# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Work-state panel reducer node."""

from omnimarket.nodes.node_work_state_panel_reducer.handlers.handler_work_state_panel_reducer import (
    HandlerWorkStatePanelReducer,
)

__all__ = [
    "HandlerWorkStatePanelReducer",
    "NodeWorkStatePanelReducer",
]


class NodeWorkStatePanelReducer(HandlerWorkStatePanelReducer):
    """ONEX entry-point wrapper for HandlerWorkStatePanelReducer."""
