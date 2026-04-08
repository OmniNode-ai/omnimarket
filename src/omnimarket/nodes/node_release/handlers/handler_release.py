"""Handler for node_release — structural placeholder."""

from omnimarket.nodes.node_release.models.model_release_state import (
    ModelReleaseCompletedEvent,
    ModelReleaseStartCommand,
)


class HandlerRelease:
    def handle(self, command: ModelReleaseStartCommand) -> ModelReleaseCompletedEvent:
        raise NotImplementedError(  # stub-ok: structural placeholder, logic migrated in follow-up
            "This node is a structural placeholder. "
            "Logic is currently in the omniclaude skill (onex:release) "
            "and will be migrated here in a follow-up ticket."
        )
