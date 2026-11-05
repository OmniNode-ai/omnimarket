# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative ownership compute node."""

from pathlib import Path

from omnimarket.models.model_lane_routing import (
    ModelLaneBlockFacts,
    ModelOwnerDiagnosis,
)

from .handler import HandlerLaneBlockDiagnoseCompute


class NodeLaneBlockDiagnoseCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def handle(self, request: ModelLaneBlockFacts) -> ModelOwnerDiagnosis:
        return HandlerLaneBlockDiagnoseCompute().handle(request)
