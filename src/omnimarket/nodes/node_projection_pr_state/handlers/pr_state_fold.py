# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B fold: no clock, transport, envelope, database or filesystem I/O."""

from datetime import datetime

from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_fold_request import (
    ModelPrStateFoldRequest,
)
from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_fold_result import (
    ModelPrStateFoldResult,
)


class HandlerProjectionPrState:
    def handle(self, request: ModelPrStateFoldRequest) -> ModelPrStateFoldResult:
        return ModelPrStateFoldResult(
            event=request.event,
            observed_at=datetime.fromisoformat(request.event.observed_at),
        )


def apply_result(
    state: dict[str, ModelPrStateFoldResult], result: ModelPrStateFoldResult
) -> None:
    """In-memory twin of the SQL guard: strict ordering by timestamp then digest."""
    current = state.get(result.key)
    if current is None or (current.observed_at, current.event.digest) < (
        result.observed_at,
        result.event.digest,
    ):
        state[result.key] = result
