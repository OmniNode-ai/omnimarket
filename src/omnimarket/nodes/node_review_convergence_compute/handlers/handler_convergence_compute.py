# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_review_convergence_compute (OMN-13210 / B1).

COMPUTE node. Eval-time tooling re-homed from the dying node_hostile_reviewer
shell. Computes per-model precision / recall / F1 of locally-detected findings
against frontier-labeled ground truth. Pure; no I/O.
"""

from __future__ import annotations

from omnimarket.nodes.node_review_convergence_compute.models.model_review_convergence import (
    ModelConvergenceInput,
    ModelConvergenceOutput,
    compute_convergence,
)


class HandlerConvergenceCompute:
    """COMPUTE: per-model precision / recall / F1 vs frontier ground truth."""

    def handle(self, payload: ModelConvergenceInput) -> ModelConvergenceOutput:
        """Compute convergence metrics. Pure; returns the result, emits nothing."""
        return compute_convergence(payload)


__all__: list[str] = ["HandlerConvergenceCompute"]
