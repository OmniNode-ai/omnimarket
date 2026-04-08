"""Handler for node_redeploy — structural placeholder."""

from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    ModelRedeployCompletedEvent,
    ModelRedeployStartCommand,
)


class HandlerRedeploy:
    def handle(self, command: ModelRedeployStartCommand) -> ModelRedeployCompletedEvent:
        raise NotImplementedError(
            "This node is a structural placeholder. "
            "Logic is currently in the omniclaude skill (onex:redeploy) "
            "and will be migrated here in a follow-up ticket."
        )
