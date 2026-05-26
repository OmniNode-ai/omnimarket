# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler stub for node_epic_team_orchestrator [OMN-12206].

ORCHESTRATOR node. Consumes ModelEpicTeamRequest, orchestrates a multi-repo
sprint: decompose epic → build DAG → dispatch waves of ticket-pipeline agents
→ monitor stalls → collect results → run DoD compliance gate.

Implementation is deferred (node_not_implemented: true). This stub raises
NotImplementedError so the runtime fails loudly rather than silently misbehaving.
"""

from __future__ import annotations

from omnimarket.nodes.node_epic_team_orchestrator.models.model_epic_team_request import (
    ModelEpicTeamRequest,
)
from omnimarket.nodes.node_epic_team_orchestrator.models.model_epic_team_result import (
    ModelEpicTeamResult,
)


class HandlerEpicTeamOrchestrator:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelEpicTeamRequest) -> ModelEpicTeamResult:
        raise NotImplementedError(  # stub-ok
            "node_epic_team_orchestrator is not yet implemented (OMN-12206). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
