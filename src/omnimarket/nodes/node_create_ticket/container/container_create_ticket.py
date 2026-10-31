# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
)


def build_handler_create_ticket() -> HandlerCreateTicket:
    """Build the handler with its constructor defaults."""
    return HandlerCreateTicket()
