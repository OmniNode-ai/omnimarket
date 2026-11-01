# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Construct the handler bound to node_umbrella_ticket_compute."""

from __future__ import annotations

from omnimarket.nodes.node_umbrella_ticket_compute.handlers.handler_umbrella_ticket import (
    HandlerUmbrellaTicket,
)

__all__ = ["create_handler_umbrella_ticket"]


def create_handler_umbrella_ticket() -> HandlerUmbrellaTicket:
    """Build the node handler with its bound dependencies."""
    return HandlerUmbrellaTicket()
