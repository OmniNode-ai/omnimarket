# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_ticket_closer_scheduled_effect: the ticket closer that runs on a declared schedule in the runtime (OMN-24138)."""

from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.handler_ticket_closer_scheduled import (
    HandlerTicketCloserScheduled,
)

__all__ = ["HandlerTicketCloserScheduled", "NodeTicketCloserScheduledEffect"]


class NodeTicketCloserScheduledEffect(HandlerTicketCloserScheduled):
    """ONEX entry-point wrapper for HandlerTicketCloserScheduled."""
