# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep direct-call exceptions intact and publish bus failures as typed terminal results."""

from omnimarket.models.model_operator_terminal_failure import (
    ModelOperatorTerminalFailure,
)
from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseClassification,
    ModelPromiseClassifyRequest,
)
from omnimarket.nodes.node_orchestrator_promise_classify_compute.handlers.handler_promise_classify import (
    HandlerPromiseClassify as BaseHandlerPromiseClassify,
)


class HandlerPromiseClassify:
    def __init__(self) -> None:
        self._handler = BaseHandlerPromiseClassify()

    def handle(
        self, request: ModelPromiseClassifyRequest
    ) -> ModelPromiseClassification | ModelOperatorTerminalFailure:
        try:
            return self._handler.handle(request)
        except Exception as exc:
            return ModelOperatorTerminalFailure(
                terminal_failure_cause=type(exc).__name__
            )
