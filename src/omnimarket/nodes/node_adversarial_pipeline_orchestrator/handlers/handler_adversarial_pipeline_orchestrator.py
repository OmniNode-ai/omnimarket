# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_adversarial_pipeline_orchestrator [OMN-12215].

ORCHESTRATOR node. Chains three background stages:
  Stage 1 — design_to_plan: produces a plan from a topic
  Stage 2 — hostile_reviewer gate: reviews plan, requires >= min_findings_gate issues
  Stage 3 — plan_to_tickets: converts the gated plan into Linear tickets

All stages dispatch background agents via dispatch_worker. No foreground implementation.

STUB — not yet implemented.
"""

from omnimarket.nodes.node_adversarial_pipeline_orchestrator.models.model_adversarial_pipeline_request import (
    ModelAdversarialPipelineRequest,
    ModelAdversarialPipelineResult,
)


class HandlerAdversarialPipelineOrchestrator:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(
        self, request: ModelAdversarialPipelineRequest
    ) -> ModelAdversarialPipelineResult:
        raise NotImplementedError(  # stub-ok
            "node_adversarial_pipeline_orchestrator is not yet implemented (OMN-12215). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
