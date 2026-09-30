# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a pure fold: no clock, database, broker or envelope."""

from omnimarket.nodes.node_projection_delegation_eval.models import (
    ModelDelegationEvalProjectionRequest,
    ModelDelegationEvalProjectionResult,
    ModelDelegationEvalRow,
)


class HandlerProjectionDelegationEval:
    """Derive exactly one row from one labelled attempt."""

    def handle(
        self, request: ModelDelegationEvalProjectionRequest
    ) -> ModelDelegationEvalProjectionResult:
        row = ModelDelegationEvalRow(
            **request.model_dump(),
            item_key=f"{request.correlation_id}:{request.attempt_index}",
        )
        return ModelDelegationEvalProjectionResult(rows=(row,))
