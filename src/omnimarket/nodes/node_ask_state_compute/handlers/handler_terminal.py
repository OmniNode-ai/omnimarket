# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep direct-call exceptions intact and publish bus failures as typed terminal results."""

from omnimarket.models.model_ask_state import ModelAskStateReport, ModelAskStateRequest
from omnimarket.models.model_operator_terminal_failure import (
    ModelOperatorTerminalFailure,
)
from omnimarket.nodes.node_ask_state_compute.handlers.handler_ask_state import (
    HandlerAskState as BaseHandlerAskState,
)


class HandlerAskState:
    def __init__(self) -> None:
        self._handler = BaseHandlerAskState()

    def handle(
        self, request: ModelAskStateRequest
    ) -> ModelAskStateReport | ModelOperatorTerminalFailure:
        try:
            return self._handler.handle(request)
        except Exception as exc:
            return ModelOperatorTerminalFailure(
                terminal_failure_cause=type(exc).__name__
            )
