# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative routing effect node."""

from pathlib import Path

from omnimarket.models.model_lane_routing import (
    ModelLaneRoutingRequest,
    ModelLaneRoutingResult,
)

from .handler import HandlerLaneBlockDiagnoseEffect


class NodeLaneBlockDiagnoseEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def handle(self, request: ModelLaneRoutingRequest) -> ModelLaneRoutingResult:
        return HandlerLaneBlockDiagnoseEffect().handle(request)
