# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plan the seam readings of the factory line from its graph (OMN-21459).

Pure: the seam nodes (COMPUTE_GENERIC) of the supplied graph nodes, in contract order, each
with the classify operation its step_config names. Gathering facts and running the readings
arrive with the seam reader tasks.
"""

from __future__ import annotations

from omnimarket.nodes.node_seam_read_orchestrator.models.model_seam_read_plan import (
    ModelSeamReadPlan,
    ModelSeamReadPlanRequest,
    ModelSeamReadStep,
)


class HandlerSeamReadPlan:
    """List the seam readings of the line, in line order."""

    def handle(self, request: ModelSeamReadPlanRequest) -> ModelSeamReadPlan:
        steps: list[ModelSeamReadStep] = []
        for node in request.graph_nodes:
            if node.get("node_type") != "COMPUTE_GENERIC":
                continue
            node_id = node.get("node_id")
            config = node.get("step_config")
            operation = config.get("operation") if isinstance(config, dict) else None
            if not isinstance(node_id, str) or not isinstance(operation, str):
                raise ValueError(f"seam node without an id and operation: {dict(node)}")
            steps.append(ModelSeamReadStep(node_id=node_id, operation=operation))
        return ModelSeamReadPlan(steps=tuple(steps))
