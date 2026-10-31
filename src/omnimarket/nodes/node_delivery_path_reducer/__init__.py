# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_delivery_path_reducer: fold the lifecycle of one delivery path."""

from omnimarket.nodes.node_delivery_path_reducer.handlers.handler_delivery_path import (
    HandlerDeliveryPath,
)

__all__ = ["HandlerDeliveryPath", "NodeDeliveryPathReducer"]


class NodeDeliveryPathReducer(HandlerDeliveryPath):
    """ONEX entry-point wrapper for HandlerDeliveryPath."""
