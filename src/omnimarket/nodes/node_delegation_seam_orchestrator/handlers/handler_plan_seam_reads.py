# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``plan_seam_reads``: name the reader operations of the delegation seam readings.

Canonical definition-B shape: ``handle(ModelSeamReadPlanRequest) -> ModelSeamReadPlan``.
The steps are the contract's edges; the readers are bound to the line as they land.
"""

from __future__ import annotations

from omnimarket.nodes.node_delegation_seam_orchestrator.models.model_seam_read_plan import (
    ModelSeamReadPlan,
    ModelSeamReadPlanRequest,
    ModelSeamReadStep,
)

_STEPS = (
    ModelSeamReadStep(
        node="node_delegation_seam_measure_compute",
        operation="load_delegation_workflows",
    ),
)


class HandlerPlanSeamReads:
    """ORCHESTRATOR: the ordered reader operations for one reading."""

    def handle(self, request: ModelSeamReadPlanRequest) -> ModelSeamReadPlan:
        return ModelSeamReadPlan(
            planned_at=request.now,
            workflow_ids=request.workflow_ids,
            steps=_STEPS,
        )
