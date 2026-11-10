# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_document_reducer: holds the latest floor document (OMN-23072)."""

from omnimarket.nodes.node_floor_document_reducer.handlers.handler_floor_document_reducer import (
    HandlerFloorDocumentReducer,
)

__all__ = ["HandlerFloorDocumentReducer", "NodeFloorDocumentReducer"]


class NodeFloorDocumentReducer(HandlerFloorDocumentReducer):
    """ONEX entry-point wrapper for HandlerFloorDocumentReducer."""
