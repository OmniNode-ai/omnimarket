# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for a refused lane pin's fallback decision."""

from pathlib import Path

from .handlers.handler_lane_pin_fallback import HandlerLanePinFallback
from .models.model_lane_pin_fallback import (
    ModelLanePinFallbackRequest,
    ModelLanePinFallbackResult,
)


class NodeLanePinFallbackCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerLanePinFallback()

    def handle(
        self, request: ModelLanePinFallbackRequest
    ) -> ModelLanePinFallbackResult:
        return self.handler.handle(request)
