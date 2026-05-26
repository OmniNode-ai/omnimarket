"""HandlerGenerateNode — handler for node_generate_node_effect.  # stub-ok

Full implementation wired in OMN-12230 follow-up work.
"""

from __future__ import annotations

from omnimarket.nodes.node_generate_node_effect.models.model_generate_node_command import (
    ModelGenerateNodeCommand,
)
from omnimarket.nodes.node_generate_node_effect.models.model_generate_node_result import (
    ModelGenerateNodeResult,
)


class HandlerGenerateNode:
    """Effect handler that scaffolds a new ONEX node.  # stub-ok

    Performs template expansion and file writes given a generate-node command.
    Raises NotImplementedError until OMN-12230 follow-up wiring lands.
    """

    def handle(
        self, command: ModelGenerateNodeCommand
    ) -> ModelGenerateNodeResult:  # stub-ok
        """Execute the generate-node effect — not yet implemented, wiring tracked in OMN-12230."""
        raise NotImplementedError(  # stub-ok
            "HandlerGenerateNode.handle is not yet implemented. "
            "Template expansion and file-write wiring is tracked in OMN-12230."
        )


__all__: list[str] = ["HandlerGenerateNode"]
