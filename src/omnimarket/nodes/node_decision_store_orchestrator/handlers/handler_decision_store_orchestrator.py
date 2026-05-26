# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_decision_store_orchestrator [OMN-12219].

ORCHESTRATOR node. Routes record/query/check-conflicts sub-operations for
architectural and design decisions.

Pipeline (record):
  1. Structural conflict check (compute, pure function — always runs)
  2. Semantic LLM review (effect, async — only if structural_confidence >= 0.6)
  3. Persist decision entry (effect)
  4. Notify via Slack gate on HIGH-severity conflicts (effect, blocks pipeline)

Pipeline (query):
  1. Query NodeDecisionStoreQueryCompute with filter params
  2. Return paginated results

Pipeline (check_conflicts):
  1. Structural conflict check only — no write, no semantic check, no events

STUB — not yet implemented.
"""

from omnimarket.nodes.node_decision_store_orchestrator.models.model_decision_store_request import (
    ModelDecisionStoreRequest,
    ModelDecisionStoreResult,
)


class HandlerDecisionStoreOrchestrator:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelDecisionStoreRequest) -> ModelDecisionStoreResult:
        raise NotImplementedError(  # stub-ok
            "node_decision_store_orchestrator is not yet implemented (OMN-12219). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
