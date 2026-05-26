"""HandlerDesignToPlanPhase3Launch — Phase 3 (launch) handler.  # stub-ok

Phase 3 executes a finalized plan by routing to downstream orchestrators
(e.g. plan_to_tickets, executing_plans). Not yet implemented — full wiring
is follow-up work tracked in OMN-12228.
"""

from __future__ import annotations

from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_command import (
    ModelDesignToPlanCommand,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
    ModelDesignToPlanState,
)


class HandlerDesignToPlanPhase3Launch:
    """Phase 3 launch handler.  # stub-ok

    Accepts a finalized plan and routes to downstream orchestrators to
    execute it. Raises NotImplementedError until OMN-12228 follow-up
    wiring lands — callers guard against this via no_launch=True.
    """

    def handle(  # stub-ok
        self,
        command: ModelDesignToPlanCommand,
        state: ModelDesignToPlanState,
    ) -> ModelDesignToPlanState:
        """Execute Phase 3 launch — not yet implemented, wiring tracked in OMN-12228."""
        raise NotImplementedError(  # stub-ok
            "HandlerDesignToPlanPhase3Launch.handle is not yet implemented. "
            "Phase 3 wiring is tracked in OMN-12228. "
            "Pass no_launch=True to skip Phase 3."
        )


__all__: list[str] = ["HandlerDesignToPlanPhase3Launch"]
