# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect node; composition supplies its reviewed overlay and ports."""

from omnimarket.models.error_rule_measurement import (
    ModelDailyMeasurementResult,
    ModelMeasurementTick,
)

from .handlers.handler_daily import HandlerErrorRuleMeasurementDaily


class NodeErrorRuleMeasurementEffect:
    def __init__(self, handler: HandlerErrorRuleMeasurementDaily) -> None:
        self.handler = handler

    def handle(self, request: ModelMeasurementTick) -> ModelDailyMeasurementResult:
        return self.handler.handle(request)
