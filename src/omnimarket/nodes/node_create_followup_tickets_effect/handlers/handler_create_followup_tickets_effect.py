"""HandlerCreateFollowupTicketsEffect — stub for Wave 2 implementation.

Single-purpose side effect: structured review findings → Linear tickets.
Full implementation (Linear API calls, priority mapping, seam detection,
ModelTicketContract injection) is deferred to Wave 2.
"""

from __future__ import annotations

from typing import Literal

from omnimarket.nodes.node_create_followup_tickets_effect.models.model_create_followup_tickets_state import (
    ModelCreateFollowupTicketsCommand,
    ModelCreateFollowupTicketsResult,
)


class HandlerCreateFollowupTicketsEffect:
    """Effect handler that converts review findings into Linear tickets.

    Wave 1: contract + stub only — raises NotImplementedError.
    Wave 2: full Linear API integration, priority mapping, seam detection.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def handle(
        self, command: ModelCreateFollowupTicketsCommand
    ) -> ModelCreateFollowupTicketsResult:
        """Convert a batch of review findings into Linear tickets.

        Args:
            command: Batch of review findings with project/team/parent targeting.

        Returns:
            Result with created ticket IDs, URLs, and any per-finding failures.

        Raises:
            NotImplementedError: Always — implementation deferred to Wave 2.
        """
        raise NotImplementedError(  # stub-ok
            "HandlerCreateFollowupTicketsEffect is a Wave 1 contract stub. "
            "Full implementation (Linear API, priority mapping, seam detection) "
            "is deferred to Wave 2 (OMN-12204)."
        )


__all__: list[str] = ["HandlerCreateFollowupTicketsEffect"]
