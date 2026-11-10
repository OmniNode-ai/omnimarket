# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the host repair dispatch node."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_lab_host_repair_dispatch_effect.handlers.handler_host_repair_dispatch import (
    HandlerHostRepairDispatch,
)
from omnimarket.nodes.node_lab_host_repair_dispatch_effect.models.model_host_repair import (
    ModelHostRepairRequest,
    ModelHostRepairResult,
)


class NodeLabHostRepairDispatchEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerHostRepairDispatch()

    def handle(self, request: ModelHostRepairRequest) -> ModelHostRepairResult:
        return self.handler.handle(request)
