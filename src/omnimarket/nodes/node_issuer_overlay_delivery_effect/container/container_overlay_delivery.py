# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire the overlay delivery handler to a host-file transport (OMN-21899)."""

from __future__ import annotations

from omnimarket.nodes.node_issuer_overlay_delivery_effect.handlers.handler_overlay_delivery import (
    HandlerOverlayDelivery,
)
from omnimarket.nodes.node_issuer_overlay_delivery_effect.protocols import (
    ProtocolIssuerHostFiles,
)


def create_handler_overlay_delivery(
    host: ProtocolIssuerHostFiles,
) -> HandlerOverlayDelivery:
    """Return the handler bound to the given host-file transport."""
    return HandlerOverlayDelivery(host)
