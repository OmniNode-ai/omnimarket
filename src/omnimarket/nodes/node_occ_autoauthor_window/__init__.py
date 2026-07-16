# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OCC auto-authoring window node (OMN-14393, report-only N=10 counter)."""

from omnimarket.nodes.node_occ_autoauthor_window.handlers.handler_occ_autoauthor_window import (
    HandlerOccAutoauthorWindow,
    aggregate_autoauthor_window,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_request import (
    ModelOccAutoauthorWindowRequest,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_result import (
    ModelOccAutoauthorWindowResult,
)


class NodeOccAutoauthorWindow(HandlerOccAutoauthorWindow):
    """ONEX entry-point wrapper for HandlerOccAutoauthorWindow (OMN-14393)."""


__all__ = [
    "HandlerOccAutoauthorWindow",
    "ModelOccAutoauthorWindowRequest",
    "ModelOccAutoauthorWindowResult",
    "NodeOccAutoauthorWindow",
    "aggregate_autoauthor_window",
]
