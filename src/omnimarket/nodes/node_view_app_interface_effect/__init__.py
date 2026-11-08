# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_view_app_interface_effect: INSUFFICIENT scaffold of the publish effect's app interface."""

from omnimarket.nodes.node_view_app_interface_effect.handlers.handler_view_app_interface_effect import (
    HandlerViewAppInterfaceEffect,
)

__all__ = ["HandlerViewAppInterfaceEffect", "NodeViewAppInterfaceEffect"]


class NodeViewAppInterfaceEffect(HandlerViewAppInterfaceEffect):
    """ONEX entry-point wrapper for HandlerViewAppInterfaceEffect."""
