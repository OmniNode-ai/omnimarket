# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lab run spill node."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_lab_run_spill_effect.handlers.handler_lab_run_spill import (
    HandlerLabRunSpill,
)
from omnimarket.nodes.node_lab_run_spill_effect.models.model_lab_run_spill import (
    ModelLabRunSpillRequest,
    ModelLabRunSpillResult,
)


class NodeLabRunSpillEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerLabRunSpill()

    def handle(self, request: ModelLabRunSpillRequest) -> ModelLabRunSpillResult:
        return self.handler.handle(request)
