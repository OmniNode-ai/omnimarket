# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scaffold handler of node_retrospective_run_reducer: carries the crossing's identity fields through, decides nothing."""

from __future__ import annotations

from omnimarket.nodes.node_retrospective_run_reducer.models.model_retrospective_run_reducer_request import (
    ModelRetrospectiveRunReducerRequest,
)
from omnimarket.nodes.node_retrospective_run_reducer.models.model_retrospective_run_reducer_result import (
    ModelRetrospectiveRunReducerResult,
)


class HandlerRetrospectiveRunReducer:
    """Fold the evaluated verdicts into the run's state.

    The scaffold copies the identity fields both crossings share and leaves every other
    field unset; the logic of this node arrives in the wave that fills its handler.
    """

    def handle(
        self, request: ModelRetrospectiveRunReducerRequest
    ) -> ModelRetrospectiveRunReducerResult:
        return ModelRetrospectiveRunReducerResult(
            run_id=request.run_id,
            correlation_id=request.correlation_id,
        )


__all__ = ["HandlerRetrospectiveRunReducer"]
