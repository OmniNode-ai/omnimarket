# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scaffold handler of node_retrospective_calculate_compute: carries the crossing's identity fields through, decides nothing."""

from __future__ import annotations

from omnimarket.nodes.node_retrospective_calculate_compute.models.model_retrospective_calculate_compute_request import (
    ModelRetrospectiveCalculateComputeRequest,
)
from omnimarket.nodes.node_retrospective_calculate_compute.models.model_retrospective_calculate_compute_result import (
    ModelRetrospectiveCalculateComputeResult,
)


class HandlerRetrospectiveCalculateCompute:
    """Calculate the cohort metrics from the evidence snapshot.

    The scaffold copies the identity fields both crossings share and leaves every other
    field unset; the logic of this node arrives in the wave that fills its handler.
    """

    def handle(
        self, request: ModelRetrospectiveCalculateComputeRequest
    ) -> ModelRetrospectiveCalculateComputeResult:
        return ModelRetrospectiveCalculateComputeResult(
            run_id=request.run_id,
            correlation_id=request.correlation_id,
            input_hash=request.input_hash,
        )


__all__ = ["HandlerRetrospectiveCalculateCompute"]
