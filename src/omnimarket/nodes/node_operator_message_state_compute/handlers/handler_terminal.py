# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep direct-call exceptions intact and publish bus failures as typed terminal results."""

from omnimarket.models.model_operator_message_state import (
    ModelOperatorMessageStateReport,
    ModelOperatorMessageStateRequest,
)
from omnimarket.models.model_operator_terminal_failure import (
    ModelOperatorTerminalFailure,
)
from omnimarket.nodes.node_operator_message_state_compute.handlers.handler_operator_message_state import (
    HandlerOperatorMessageState as BaseHandlerOperatorMessageState,
)


class HandlerOperatorMessageState:
    def __init__(self) -> None:
        self._handler = BaseHandlerOperatorMessageState()

    def handle(
        self, request: ModelOperatorMessageStateRequest
    ) -> ModelOperatorMessageStateReport | ModelOperatorTerminalFailure:
        try:
            return self._handler.handle(request)
        except Exception as exc:
            return ModelOperatorTerminalFailure(
                terminal_failure_cause=type(exc).__name__
            )
