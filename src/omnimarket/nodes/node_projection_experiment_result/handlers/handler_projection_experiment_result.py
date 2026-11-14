# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a pure fold of one recorded experiment-arm result: no clock, database, broker or envelope."""

from omnimarket.nodes.node_projection_experiment_result.models import (
    ModelExperimentResultProjectionRequest,
    ModelExperimentResultProjectionResult,
    ModelExperimentResultRow,
)


class HandlerProjectionExperimentResult:
    """Derive exactly one row from one scored arm.

    accuracy is correct / n; advantage_over_baseline is accuracy minus the
    majority-answer baseline; clears_baseline is the WHOLE 95 percent interval
    sitting above the baseline (wilson_low > baseline_accuracy), not the point
    estimate. Every other field is copied from request.result unchanged.
    """

    def handle(
        self, request: ModelExperimentResultProjectionRequest
    ) -> ModelExperimentResultProjectionResult:
        result = request.result
        accuracy = result.correct / result.n
        row = ModelExperimentResultRow(
            **result.model_dump(),
            accuracy=accuracy,
            advantage_over_baseline=accuracy - result.baseline_accuracy,
            clears_baseline=result.wilson_low > result.baseline_accuracy,
        )
        return ModelExperimentResultProjectionResult(row=row)
