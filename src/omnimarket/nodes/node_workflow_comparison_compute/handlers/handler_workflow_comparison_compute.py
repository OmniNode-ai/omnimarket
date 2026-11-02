# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scaffold handler of node_workflow_comparison_compute: carries the crossing's identity fields through, decides nothing."""

from __future__ import annotations

from omnimarket.nodes.node_workflow_comparison_compute.models.model_workflow_comparison_compute_request import (
    ModelWorkflowComparisonComputeRequest,
)
from omnimarket.nodes.node_workflow_comparison_compute.models.model_workflow_comparison_compute_result import (
    ModelWorkflowComparisonComputeResult,
)


class HandlerWorkflowComparisonCompute:
    """Evaluate every experiment's success contract against the audited snapshot.

    The scaffold copies the identity fields both crossings share and leaves every other
    field unset; the logic of this node arrives in the wave that fills its handler.
    """

    def handle(
        self, request: ModelWorkflowComparisonComputeRequest
    ) -> ModelWorkflowComparisonComputeResult:
        return ModelWorkflowComparisonComputeResult(
            run_id=request.run_id,
            correlation_id=request.correlation_id,
            input_hash=request.input_hash,
        )


__all__ = ["HandlerWorkflowComparisonCompute"]
