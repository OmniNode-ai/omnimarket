# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_view_seam_reading_reducer: project the seam family into the view_seam_readings exposure."""

from omnimarket.nodes.node_view_seam_reading_reducer.handlers.handler_view_seam_reading_reducer import (
    HandlerViewSeamReadingReducer,
)

__all__ = ["HandlerViewSeamReadingReducer", "NodeViewSeamReadingReducer"]


class NodeViewSeamReadingReducer(HandlerViewSeamReadingReducer):
    """ONEX entry-point wrapper for HandlerViewSeamReadingReducer."""
