# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler factories for node_ticket_text_effect."""

from __future__ import annotations

from omnimarket.nodes.node_ticket_text_effect.handlers.handler_ticket_text import (
    HandlerTicketText,
)

__all__ = ["create_handler_ticket_text"]


def create_handler_ticket_text() -> HandlerTicketText:
    """Build the handler with its default dependencies."""
    return HandlerTicketText()
