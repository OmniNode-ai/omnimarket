# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the seam graph's defect derivation (OMN-19998)."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.model_seam_graph_defects import (
    ModelSeamGraphDefectReport,
    ModelSeamGraphRequest,
)

from .handlers.handler_seam_graph_defects import HandlerSeamGraphDefects


class NodeSeamGraphDefectsCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerSeamGraphDefects()

    def handle(self, request: ModelSeamGraphRequest) -> ModelSeamGraphDefectReport:
        return self.handler.handle(request)
