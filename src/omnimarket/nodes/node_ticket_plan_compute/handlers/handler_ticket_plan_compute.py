# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTicketPlanCompute — STUB.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
Ticket: OMN-12233
"""

from __future__ import annotations

from omnimarket.nodes.node_ticket_plan_compute.models.model_ticket_plan_request import (
    ModelTicketPlanRequest,
)
from omnimarket.nodes.node_ticket_plan_compute.models.model_ticket_plan_result import (
    ModelTicketPlanResult,
)


class HandlerTicketPlanCompute:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelTicketPlanRequest) -> ModelTicketPlanResult:
        raise NotImplementedError(  # stub-ok
            "node_ticket_plan_compute is not yet implemented (OMN-12233). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
