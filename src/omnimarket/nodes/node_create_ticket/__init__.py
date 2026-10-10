"""node_create_ticket — the one Linear ticket node: create, read and comment."""

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
)
from omnimarket.nodes.node_create_ticket.models.model_create_ticket_state import (
    ModelCreateTicketCompletedEvent,
)

__all__ = [
    "HandlerCreateTicket",
    "ModelCreateTicketCompletedEvent",
    "NodeCreateTicket",
]


class NodeCreateTicket(HandlerCreateTicket):
    """ONEX entry-point wrapper for HandlerCreateTicket."""
