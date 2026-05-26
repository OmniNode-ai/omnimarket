# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerInsightsToPlanCompute — HTML parse → extract insights → structure plan.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
STUB — not yet implemented (OMN-12231).
"""

from __future__ import annotations

from omnimarket.nodes.node_insights_to_plan_compute.models.model_insights_to_plan_compute_request import (
    ModelInsightsToPlanComputeRequest,
)
from omnimarket.nodes.node_insights_to_plan_compute.models.model_insights_to_plan_compute_result import (
    ModelInsightsToPlanComputeResult,
)


class HandlerInsightsToPlanCompute:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(
        self, request: ModelInsightsToPlanComputeRequest
    ) -> ModelInsightsToPlanComputeResult:
        raise NotImplementedError(  # stub-ok
            "node_insights_to_plan_compute is not yet implemented (OMN-12231). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
