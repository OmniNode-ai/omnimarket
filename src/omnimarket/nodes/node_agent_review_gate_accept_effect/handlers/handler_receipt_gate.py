# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical receipt-only operation of the existing gate-accept node."""

from ..models.model_gate_accept import ModelGateAcceptResult, ModelReceiptGateRequest
from .handler_gate_accept import HandlerGateAccept


class HandlerReceiptGate:
    def __init__(self, handler: HandlerGateAccept | None = None) -> None:
        self._handler = handler if handler is not None else HandlerGateAccept()

    def handle(self, request: ModelReceiptGateRequest) -> ModelGateAcceptResult:
        return self._handler.handle_receipt(request)
