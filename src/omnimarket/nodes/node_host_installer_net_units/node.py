# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the Linux host net unit installer."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_installer_net_units.handlers.handler_net_units import (
    HandlerNetUnits,
)
from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
    ModelNetUnitsRequest,
    ModelNetUnitsResult,
)


class NodeHostInstallerNetUnits:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerNetUnits()

    def handle(self, request: ModelNetUnitsRequest) -> ModelNetUnitsResult:
        return self.handler.handle(request)
