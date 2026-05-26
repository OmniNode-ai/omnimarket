# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_friction_triage_orchestrator [OMN-12205].

ORCHESTRATOR node. Reads the friction registry (NDJSON), aggregates events
by skill:surface over a rolling window, deduplicates against open Linear tickets
via stable surface-key markers, and creates new tickets for surfaces crossing
threshold (count >= 3 OR severity_score >= 9).

STUB — not yet implemented.
"""

from omnimarket.nodes.node_friction_triage_orchestrator.models.model_friction_triage_request import (
    ModelFrictionTriageRequest,
    ModelFrictionTriageResult,
)


class HandlerFrictionTriageOrchestrator:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelFrictionTriageRequest) -> ModelFrictionTriageResult:
        raise NotImplementedError(  # stub-ok
            "node_friction_triage_orchestrator is not yet implemented (OMN-12205). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
