# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_review_convergence_compute (OMN-13210 / B1).

COMPUTE node. Eval-time tooling re-homed from the dying node_hostile_reviewer
shell. Computes per-model precision / recall / F1 of locally-detected findings
against frontier-labeled ground truth. Pure; no I/O.
"""

from __future__ import annotations

from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_review_convergence_compute.models.model_review_convergence import (
    ModelConvergenceInput,
    ModelConvergenceOutput,
    compute_convergence,
)

_HANDLER_ID = "node_review_convergence_compute"


class HandlerConvergenceCompute:
    """COMPUTE: per-model precision / recall / F1 vs frontier ground truth."""

    async def handle(
        self, request: ModelConvergenceInput
    ) -> ModelHandlerOutput[ModelConvergenceOutput]:
        """Compute convergence metrics. Pure; returns the result, emits nothing."""
        result = compute_convergence(request)
        return ModelHandlerOutput.for_compute(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id=_HANDLER_ID,
            result=result,
        )


__all__: list[str] = ["HandlerConvergenceCompute"]
