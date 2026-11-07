# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-ledger reducer node's one entry: route a request to the judge, the replay or the rebuild (OMN-22915).

The handlers it calls are pure and hold the logic; this class only picks one by the request's operation. A
shadow judge is stateless here (the caller supplies the ledger text before the row); the stateful
``observe`` form of the shadow handler is for a caller that owns a session.
"""

from __future__ import annotations

from omnimarket.nodes.node_work_ledger_reducer_compute.handlers.handler_work_ledger_guard_replay import (
    HandlerWorkLedgerGuardReplay,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.handlers.handler_work_ledger_rebuild import (
    HandlerWorkLedgerRebuild,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.handlers.handler_work_ledger_shadow_judge import (
    HandlerWorkLedgerShadowJudge,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_reducer import (
    ModelWorkLedgerReducerRequest,
    ModelWorkLedgerReducerResult,
)


class HandlerWorkLedgerReducer:
    def handle(
        self, request: ModelWorkLedgerReducerRequest
    ) -> ModelWorkLedgerReducerResult:
        if request.operation == "shadow_judge" and request.shadow is not None:
            return ModelWorkLedgerReducerResult(
                operation=request.operation,
                shadow=HandlerWorkLedgerShadowJudge().handle(request.shadow),
            )
        if request.operation == "guard_replay" and request.replay is not None:
            return ModelWorkLedgerReducerResult(
                operation=request.operation,
                replay=HandlerWorkLedgerGuardReplay().handle(request.replay),
            )
        if request.operation == "rebuild" and request.rebuild is not None:
            return ModelWorkLedgerReducerResult(
                operation=request.operation,
                rebuild=HandlerWorkLedgerRebuild().handle(request.rebuild),
            )
        raise ValueError(
            f"operation {request.operation} has no request"
        )  # unreachable past the model
