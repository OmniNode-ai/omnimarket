# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local ONEX wrapper for node_umbrella_ticket_compute."""

from omnimarket.nodes.node_umbrella_ticket_compute.handlers.handler_umbrella_ticket import (
    HandlerUmbrellaTicket,
)


class NodeUmbrellaTicketCompute(HandlerUmbrellaTicket):
    """ONEX entry-point wrapper for HandlerUmbrellaTicket."""


__all__ = ["HandlerUmbrellaTicket", "NodeUmbrellaTicketCompute"]
