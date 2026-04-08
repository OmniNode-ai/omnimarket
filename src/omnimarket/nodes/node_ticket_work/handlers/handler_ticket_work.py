"""Handler for node_ticket_work — structural placeholder."""

from omnimarket.nodes.node_ticket_work.models.model_ticket_work_state import (
    ModelTicketWorkCompletedEvent,
    ModelTicketWorkStartCommand,
)


class HandlerTicketWork:
    def handle(
        self, command: ModelTicketWorkStartCommand
    ) -> ModelTicketWorkCompletedEvent:
        raise NotImplementedError(  # stub-ok: structural placeholder, logic migrated in follow-up
            "This node is a structural placeholder. "
            "Logic is currently in the omniclaude skill (onex:ticket_work) "
            "and will be migrated here in a follow-up ticket."
        )
