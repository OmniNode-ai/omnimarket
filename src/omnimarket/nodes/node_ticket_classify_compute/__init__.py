# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ticket classify compute node — keyword heuristic buildability classification."""

from omnimarket.nodes.node_ticket_classify_compute.handlers.handler_ticket_classify import (
    HandlerTicketClassify,
)

__all__ = [
    "HandlerTicketClassify",
    "NodeTicketClassifyCompute",
]


class NodeTicketClassifyCompute(HandlerTicketClassify):
    """ONEX entry-point wrapper for HandlerTicketClassify."""
