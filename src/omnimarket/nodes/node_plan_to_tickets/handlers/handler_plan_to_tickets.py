"""Handler for node_plan_to_tickets — structural placeholder."""

from omnimarket.nodes.node_plan_to_tickets.models.model_plan_to_tickets_state import (
    ModelPlanToTicketsCompletedEvent,
    ModelPlanToTicketsStartCommand,
)


class HandlerPlanToTickets:
    def handle(
        self, command: ModelPlanToTicketsStartCommand
    ) -> ModelPlanToTicketsCompletedEvent:
        raise NotImplementedError(  # stub-ok: structural placeholder, logic migrated in follow-up
            "This node is a structural placeholder. "
            "Logic is currently in the omniclaude skill (onex:plan_to_tickets) "
            "and will be migrated here in a follow-up ticket."
        )
