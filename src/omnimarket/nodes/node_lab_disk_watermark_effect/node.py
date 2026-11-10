# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lab disk watermark node."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark import (
    HandlerLabDiskWatermark,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkReading,
    ModelLabDiskWatermarkRequest,
)


class NodeLabDiskWatermarkEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerLabDiskWatermark()

    def handle(
        self, request: ModelLabDiskWatermarkRequest
    ) -> ModelLabDiskWatermarkReading:
        return self.handler.handle(request)
