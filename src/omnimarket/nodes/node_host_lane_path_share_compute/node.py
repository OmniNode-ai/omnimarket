# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lane path share check."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_lane_path_share_compute.handlers.handler_lane_path_share import (
    HandlerLanePathShare,
)
from omnimarket.nodes.node_host_lane_path_share_compute.models.model_lane_path_share import (
    ModelLanePathShareRequest,
    ModelLanePathShareResult,
)


class NodeHostLanePathShareCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerLanePathShare()

    def handle(self, request: ModelLanePathShareRequest) -> ModelLanePathShareResult:
        return self.handler.handle(request)
