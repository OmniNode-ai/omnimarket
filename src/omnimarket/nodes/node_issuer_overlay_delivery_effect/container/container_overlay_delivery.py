# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire the overlay delivery handler to the container that holds the host-file transport (OMN-21899)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnimarket.nodes.node_issuer_overlay_delivery_effect.handlers.handler_overlay_delivery import (
    HandlerOverlayDelivery,
)

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer


def create_handler_overlay_delivery(
    container: ModelONEXContainer,
) -> HandlerOverlayDelivery:
    """Return the handler bound to a container that serves ProtocolIssuerHostFiles."""
    return HandlerOverlayDelivery(container)
