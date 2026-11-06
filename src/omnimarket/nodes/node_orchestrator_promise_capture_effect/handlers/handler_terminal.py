# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve effect dependencies inside the bus failure boundary, after wiring."""

from functools import partial
from threading import Lock
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
        # Runtime discovery must be able to wire the failure route even when a
        # deployment overlay is absent or invalid. Resolve it on the first call;
        # a refusal stays retryable and cannot quarantine the node at boot.
        self._build_handler = partial(
            BaseHandlerPromiseCapture,
            store=store,
            classifier=classifier,
            delegate=delegate,
            overlay=overlay,
            evaluation=evaluation,
        )
        self._handler: BaseHandlerPromiseCapture | None = None
        self._lock = Lock()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(
        self, request: ModelPromiseCaptureRecord
    ) -> ModelPromiseCaptureReceipt | ModelOperatorTerminalFailure:
        try:
            with self._lock:
                if self._handler is None:
                    self._handler = self._build_handler()
                handler = self._handler
            return handler.handle(request)
        except Exception as exc:
            return ModelOperatorTerminalFailure(
                terminal_failure_cause=type(exc).__name__
            )
