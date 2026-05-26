# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFeatureDashboardCompute — STUB.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
Ticket: OMN-12229
"""

from __future__ import annotations

from omnimarket.nodes.node_feature_dashboard_compute.models.model_feature_dashboard_request import (
    ModelFeatureDashboardRequest,
)
from omnimarket.nodes.node_feature_dashboard_compute.models.model_feature_dashboard_result import (
    ModelFeatureDashboardResult,
)


class HandlerFeatureDashboardCompute:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(
        self, request: ModelFeatureDashboardRequest
    ) -> ModelFeatureDashboardResult:
        raise NotImplementedError(  # stub-ok
            "node_feature_dashboard_compute is not yet implemented (OMN-12229). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
