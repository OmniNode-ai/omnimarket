"""Handler for node_design_to_plan — structural placeholder."""

from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
    ModelDesignToPlanCompletedEvent,
    ModelDesignToPlanStartCommand,
)


class HandlerDesignToPlan:
    def handle(
        self, command: ModelDesignToPlanStartCommand
    ) -> ModelDesignToPlanCompletedEvent:
        raise NotImplementedError(  # stub-ok: structural placeholder, logic migrated in follow-up
            "This node is a structural placeholder. "
            "Logic is currently in the omniclaude skill (onex:design_to_plan) "
            "and will be migrated here in a follow-up ticket."
        )
