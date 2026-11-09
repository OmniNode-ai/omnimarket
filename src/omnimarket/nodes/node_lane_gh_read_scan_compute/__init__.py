# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lane-text gh-read scan (OMN-18946)."""

from pathlib import Path

from .handlers.handler_gh_read_scan import HandlerGhReadScan
from .models.model_gh_read_scan import ModelGhReadScanRequest, ModelGhReadScanResult


class NodeLaneGhReadScanCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerGhReadScan()

    def handle(self, request: ModelGhReadScanRequest) -> ModelGhReadScanResult:
        return self.handler.handle(request)
