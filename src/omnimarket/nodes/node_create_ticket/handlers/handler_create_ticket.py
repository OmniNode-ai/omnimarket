"""Handler for node_create_ticket — structural placeholder."""

from omnimarket.nodes.node_create_ticket.models.model_create_ticket_state import (
    ModelCreateTicketCompletedEvent,
    ModelCreateTicketStartCommand,
)


class HandlerCreateTicket:
    def handle(
        self, command: ModelCreateTicketStartCommand
    ) -> ModelCreateTicketCompletedEvent:
        raise NotImplementedError(  # stub-ok: structural placeholder, logic migrated in follow-up
            "This node is a structural placeholder. "
            "Logic is currently in the omniclaude skill (onex:create_ticket) "
            "and will be migrated here in a follow-up ticket."
        )
