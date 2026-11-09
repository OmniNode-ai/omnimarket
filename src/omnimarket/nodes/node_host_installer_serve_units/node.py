# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lab-work serve unit installer."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_installer_serve_units.handlers.handler_serve_units import (
    HandlerServeUnits,
)
from omnimarket.nodes.node_host_installer_serve_units.models.model_serve_units import (
    ModelServeUnitsRequest,
    ModelServeUnitsResult,
)


class NodeHostInstallerServeUnits:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerServeUnits()

    def handle(self, request: ModelServeUnitsRequest) -> ModelServeUnitsResult:
        return self.handler.handle(request)
