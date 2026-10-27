# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thin lane report relay node; the copy and its checks live in the handler."""

from pathlib import Path

from .handlers.handler_lane_report_relay import HandlerLaneReportRelay
from .models.model_lane_report_relay import (
    ModelLaneReportRelayRequest,
    ModelLaneReportRelayResult,
)


class NodeLaneReportRelayEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def handle(
        self, request: ModelLaneReportRelayRequest
    ) -> ModelLaneReportRelayResult:
        return HandlerLaneReportRelay().handle(request)
