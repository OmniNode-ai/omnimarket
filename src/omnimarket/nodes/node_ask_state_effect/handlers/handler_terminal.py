# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep direct-call exceptions intact and publish bus failures as typed terminal results."""

from collections.abc import Callable

from omnimarket.models.model_ask_state import ModelAskStateReport, ModelAskStateRequest
from omnimarket.models.model_carry_over import (
    ModelCarryOverPolicy,
    ModelCarryOverRequest,
    ModelCarryOverResult,
)
from omnimarket.models.model_operator_terminal_failure import (
    ModelOperatorTerminalFailure,
)
from omnimarket.nodes.node_ask_state_effect.handlers.handler_ask_state_report import (
    HandlerAskStateReport,
)
from omnimarket.nodes.node_ask_state_effect.handlers.handler_carry_over import (
    HandlerCarryOver as BaseHandlerCarryOver,
)


class HandlerCarryOver:
    def __init__(
        self,
        append: Callable[[str], tuple[bool, str]] | None = None,
        reader: HandlerAskStateReport | None = None,
        fold: Callable[[ModelAskStateRequest], ModelAskStateReport] | None = None,
        policy: ModelCarryOverPolicy | None = None,
    ) -> None:
        self._handler = BaseHandlerCarryOver(
            append=append, reader=reader, fold=fold, policy=policy
        )

    def handle(
        self, request: ModelCarryOverRequest
    ) -> ModelCarryOverResult | ModelOperatorTerminalFailure:
        try:
            return self._handler.handle(request)
        except Exception as exc:
            return ModelOperatorTerminalFailure(
                terminal_failure_cause=type(exc).__name__
            )
