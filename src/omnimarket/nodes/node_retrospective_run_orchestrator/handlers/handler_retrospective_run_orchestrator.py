# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scaffold handler of node_retrospective_run_orchestrator: carries the crossing's identity fields through, decides nothing."""

from __future__ import annotations

from omnimarket.nodes.node_retrospective_run_orchestrator.models.model_retrospective_run_orchestrator_request import (
    ModelRetrospectiveRunOrchestratorRequest,
)
from omnimarket.nodes.node_retrospective_run_orchestrator.models.model_retrospective_run_orchestrator_result import (
    ModelRetrospectiveRunOrchestratorResult,
)


class HandlerRetrospectiveRunOrchestrator:
    """Open one retrospective run for a review week and ask for its evidence.

    The scaffold copies the identity fields both crossings share and leaves every other
    field unset; the logic of this node arrives in the wave that fills its handler.
    """

    def handle(
        self, request: ModelRetrospectiveRunOrchestratorRequest
    ) -> ModelRetrospectiveRunOrchestratorResult:
        return ModelRetrospectiveRunOrchestratorResult(
            run_id=request.run_id,
            correlation_id=request.correlation_id,
        )


__all__ = ["HandlerRetrospectiveRunOrchestrator"]
