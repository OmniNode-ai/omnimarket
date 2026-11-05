# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed event-bus entry to the existing exception queue effect."""

from omnimarket.models.model_exception_emit_wire import (
    ModelExceptionEmitWire,
    ModelExceptionEmitWireResult,
)

from ..handler import HandlerExceptionEmit


class HandlerExceptionEmitWire:
    """Keep queue decisions in the existing handler; coerce only at the bus seam."""

    def __init__(self) -> None:
        self._emit = HandlerExceptionEmit()

    def handle(self, request: ModelExceptionEmitWire) -> ModelExceptionEmitWireResult:
        return ModelExceptionEmitWireResult(self._emit.handle(request.root))
