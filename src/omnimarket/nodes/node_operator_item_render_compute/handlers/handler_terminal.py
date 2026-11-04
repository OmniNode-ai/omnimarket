# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep direct-call exceptions intact and publish bus failures as typed terminal results."""

from omnimarket.models.model_operator_item_render import (
    ModelOperatorItemRenderRequest,
    ModelOperatorItemRenderResult,
)
from omnimarket.models.model_operator_terminal_failure import (
    ModelOperatorTerminalFailure,
)
from omnimarket.nodes.node_operator_item_render_compute.handlers.handler_operator_item_render import (
    HandlerOperatorItemRender as BaseHandlerOperatorItemRender,
)


class HandlerOperatorItemRender:
    def __init__(self) -> None:
        self._handler = BaseHandlerOperatorItemRender()

    def handle(
        self, request: ModelOperatorItemRenderRequest
    ) -> ModelOperatorItemRenderResult | ModelOperatorTerminalFailure:
        try:
            return self._handler.handle(request)
        except Exception as exc:
            return ModelOperatorTerminalFailure(
                terminal_failure_cause=type(exc).__name__
            )
