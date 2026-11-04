# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep direct-call exceptions intact and publish bus failures as typed terminal results."""

from typing import Literal

from omnimarket.models.model_operator_terminal_failure import (
    ModelOperatorTerminalFailure,
)
from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseBarReport,
    ModelPromiseCaptureReceipt,
    ModelPromiseCaptureRecord,
    ModelPromiseEffectOverlay,
)
from omnimarket.nodes.node_orchestrator_promise_capture_effect.handlers.handler_promise_capture import (
    HandlerPromiseCapture as BaseHandlerPromiseCapture,
)
from omnimarket.nodes.node_orchestrator_promise_capture_effect.protocols import (
    ProtocolPromiseClassifier,
    ProtocolPromiseStore,
)
from omnimarket.protocols.protocol_promise_delegate import ProtocolPromiseDelegate


class HandlerPromiseCapture:
    def __init__(
        self,
        *,
        store: ProtocolPromiseStore | None = None,
        classifier: ProtocolPromiseClassifier | None = None,
        delegate: ProtocolPromiseDelegate | None = None,
        overlay: ModelPromiseEffectOverlay | None = None,
        evaluation: ModelPromiseBarReport | None = None,
    ) -> None:
        self._handler = BaseHandlerPromiseCapture(
            store=store,
            classifier=classifier,
            delegate=delegate,
            overlay=overlay,
            evaluation=evaluation,
        )

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return self._handler.handler_type

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return self._handler.handler_category

    def handle(
        self, request: ModelPromiseCaptureRecord
    ) -> ModelPromiseCaptureReceipt | ModelOperatorTerminalFailure:
        try:
            return self._handler.handle(request)
        except Exception as exc:
            return ModelOperatorTerminalFailure(
                terminal_failure_cause=type(exc).__name__
            )
